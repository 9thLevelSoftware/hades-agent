"""Profile-local orchestration for auto-routing planning and activation."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import math
import os
import re
import tempfile
import time
import uuid
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from agent.reasoning_support import resolve_reasoning_support
from agent.runtime_routing import (
    AgentRuntimePlan,
    AgentRuntimeRequest,
    ManualRuntimePinRequest,
    RuntimeSessionContinuation,
)
from cron.jobs import (
    get_job,
    locked_cron_store,
    locked_cron_store_strict,
    parse_schedule,
    use_cron_store,
)
from hades_constants import (
    effective_generic_reasoning_effort,
    get_config_path,
    get_hades_home,
    resolve_reasoning_config,
)
from utils import fast_safe_load

from .adapters.base import (
    PERSISTED_RUNTIME_PROJECTION_CONTRACT,
    PersistedRuntimeProjection,
)
from .advisor import Advisor, AdvisorRankingRequest, AdvisorRequest, ProposalRequest
from .catalog import CatalogService, JsonCatalogSource
from .classifier import StructuredTaskClassifier
from .config import (
    authority_document,
    authority_revision,
    config_document,
    config_revision,
    management_authority_revision,
    parse_config,
)
from .config_io import (
    ConfigConflict,
    ManagementActivationRollover,
    ManagementRevisionResult,
    _freeze_management_recovery,
    _management_backup_path,
    _management_receipt_id,
    apply_management_config_revision,
    locked_update,
    management_config_recovery_complete,
    preview_update,
    profile_config_lock,
    recover_management_config_revision,
)
from .decisions import DecisionBuilder
from .eligibility import (
    runtime_capability_rejection_reasons,
    runtime_policy_rejection_reasons,
)
from .adaptation import (
    canary_eligible,
    deterministic_canary_arm,
    materialize_profiles,
    operation_identity_hash,
    static_adaptive_revision_id,
    validate_overlay,
)
from .evidence import (
    TurnOutcomeObserverPayload,
    build_context_bucket,
    build_feedback_event,
    normalize_turn_outcome,
    turn_evidence_id,
)
from .explain import serialize_decision_explanation
from .inventory import (
    ExecutableRuntime,
    InventoryService,
    InventorySnapshot,
    ReasonCodes,
    management_inventory_ineligibility_reasons,
    verified_inventory_candidates,
)
from .management_cron import (
    MANAGEMENT_CRON_NAME,
    MANAGEMENT_SCRIPT_NAME,
    ManagementCronInstall,
    assert_management_scheduled_invocation,
    install_management_cron,
    remove_management_cron,
    rollback_management_cron_install,
)
from .management import plan_management_revision, rank_management_candidates
from .models import (
    MAX_TASK_INDEX,
    REASONING_EFFORT_ORDER,
    ActivationSettings,
    AdaptationSettings,
    AdaptiveExplanation,
    AdaptiveCanaryAssignment,
    AdaptiveLifecycleEvent,
    AdaptiveOverlay,
    AdaptiveProfileRevision,
    AdaptiveRevision,
    AutoRoutingConfig,
    AutonomousProfileManagementSettings,
    ClassifierSettings,
    ComplexityBands,
    EvidenceFeedbackValue,
    EvidenceEvent,
    LocalModelRequirements,
    ManagementCanaryAssignment,
    ManagementConfigReceipt,
    ManagementControl,
    ManagementDecisionSnapshot,
    ManagementLifecycleFinalization,
    ManagementLifecycleEvent,
    ManagementPatch,
    ManagementProfileState,
    ManagementRevision,
    PluginLlmAuthority,
    PolicyEnvelope,
    ProfileMatch,
    ReasoningEffort,
    ReasoningBounds,
    RouteProfile,
    RankingPackTrust,
    RoutingDecision,
    RoutingScopes,
    RoutingTarget,
    RuntimeKey,
)
from .learner import promotion_decision, rollback_decision, summarize_quality
from .profile_key import (
    ensure_profile_canary_key,
    ensure_profile_credential_fingerprint_key,
)
from .ranking_pack import (
    RankingPackError,
    VerifiedRankingPack,
    load_verified_ranking_pack,
    ranking_trust_summary,
    ranking_pack_status,
)
from .rules import (
    assess_with_rules,
    evaluate_rules,
    extract_task_facts,
    task_facts_hash,
)
from .selector import SelectionResult, StaticSelector
from .storage import (
    ActivationReceipt,
    DecisionCandidate,
    EvidenceCommit,
    InvalidLifecycleTransition,
    ProfileFrozen,
    ProfileStateConflict,
    RoutingStore,
    RuntimeRoutingPending,
    candidate_id_for,
)

BASELINE_CREATED_AT = "1970-01-01T00:00:00Z"
OBSERVATION_FRESHNESS = timedelta(hours=24)
_EXPLAIN_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:/@+\-]{1,256}$")


class AutoRoutingServiceError(RuntimeError):
    """An auto-routing command could not complete its validated operation."""


@dataclass(frozen=True)
class AdaptationAdvance:
    """Finite result from one bounded profile-local optimizer attempt."""

    action: str
    reason: str
    revision_id: str | None = None
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class ManagementAdvance:
    """Finite result from one independent management lifecycle attempt."""

    action: str
    reason: str
    revision_id: str | None = None
    retry_after_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ManagementProfileReconcileResult:
    """Content-free result for one independently leased profile."""

    profile_id: str
    changed: bool
    reason_code: str
    revision_id: str | None = None


@dataclass(frozen=True, slots=True)
class ManagementReconcileReport:
    """Bounded aggregate from one local management reconciliation pass."""

    changed: bool
    reason_code: str
    revision_id: str | None
    profiles: tuple[ManagementProfileReconcileResult, ...]
    scheduled: bool
    reconciled_at: str

    @classmethod
    def hold(
        cls,
        reason_code: str,
        *,
        now: datetime | None = None,
        scheduled: bool = False,
    ) -> "ManagementReconcileReport":
        moment = _management_utc_now(now)
        return cls(
            changed=False,
            reason_code=reason_code,
            revision_id=None,
            profiles=(),
            scheduled=scheduled,
            reconciled_at=_management_timestamp(moment),
        )


@dataclass(frozen=True, slots=True)
class ManagementPlanPreview:
    """Content-free identity for one read-only prepared management plan."""

    plan_id: str
    reason_code: str
    profile_ids: tuple[str, ...]
    planned_at: str


@dataclass(frozen=True, slots=True)
class _ScheduledManagementInvocation:
    """One process-local authorization binding for a scheduled reconcile."""

    authority_id: str
    management_authority_id: str
    management_control_generation: int
    cron_job_id: str
    cron_job_fingerprint: str
    one_shot_job_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class _PreparedManagementPlan:
    profile_id: str
    proposal: AutoRoutingConfig
    revision: ManagementRevision
    control_revision: ManagementRevision | None
    expected_authority_id: str
    expected_control_generation: int
    planned_at: datetime


def _management_utc_now(value: datetime | None) -> datetime:
    moment = datetime.now(UTC) if value is None else value
    if moment.tzinfo is None or moment.utcoffset() is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _management_timestamp(value: datetime) -> str:
    return _management_utc_now(value).isoformat().replace("+00:00", "Z")


class _ManagementHoldError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _checksum(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


@dataclass
class AutoRoutingService:
    """Profile-local service boundary shared by the CLI and later adapters."""

    plugin_context: Any
    hermes_home: Path
    store: RoutingStore
    adapter: Any = None
    _fault_injector: Any = field(default=None, repr=False)
    _pinned_config_path: Path | None = field(default=None, repr=False)
    _incomplete_config_apply: bool = False
    _recovery_error: str | None = None
    _management_plans: dict[str, _PreparedManagementPlan] = field(
        default_factory=dict,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self._pinned_config_path is None:
            self._pinned_config_path = Path(get_config_path()).expanduser().absolute()

    @classmethod
    def from_plugin_context(
        cls,
        ctx: Any,
        *,
        fault_injector: Any = None,
        adapter: Any = None,
        allow_cross_thread_close: bool = False,
        hermes_home: str | Path | None = None,
        config_path: str | Path | None = None,
    ) -> "AutoRoutingService":
        explicit_home = hermes_home is not None
        home = Path(
            hermes_home if explicit_home else get_hades_home()
        ).expanduser()
        if explicit_home:
            home = home.resolve()
        pinned_config_path = Path(
            config_path
            if config_path is not None
            else (home / "config.yaml" if explicit_home else get_config_path())
        ).expanduser().absolute()
        fingerprint_key = ensure_profile_credential_fingerprint_key(
            home,
            config_path=pinned_config_path,
        )
        if adapter is None:
            from .adapters.hermes_0_18 import Hermes018Adapter

            adapter = Hermes018Adapter(
                credential_fingerprint_key=fingerprint_key,
            )
        service = cls(
            plugin_context=ctx,
            hermes_home=home,
            store=RoutingStore.open(
                home=home,
                allow_cross_thread_close=allow_cross_thread_close,
            ),
            adapter=adapter,
            _fault_injector=fault_injector,
            _pinned_config_path=pinned_config_path,
        )
        service._recover_pending_applies()
        return service

    @property
    def config_path(self) -> Path:
        assert self._pinned_config_path is not None
        return self._pinned_config_path

    def _assert_profile_isolation(self) -> None:
        home = self.hermes_home.resolve()
        paths = (self.config_path.resolve(strict=False), self.store.path.resolve())
        if any(not path.is_relative_to(home) for path in paths):
            raise AutoRoutingServiceError(
                "auto-routing config and state must stay inside the active profile"
            )

    def load_proposal(self, path: str | Path) -> AutoRoutingConfig:
        proposal_path = Path(path)
        try:
            raw = fast_safe_load(proposal_path.read_bytes())
        except Exception as error:
            raise AutoRoutingServiceError(
                f"proposal could not be read: {proposal_path}"
            ) from error
        if not isinstance(raw, Mapping):
            raise AutoRoutingServiceError("proposal must contain a mapping")
        if "plugins" in raw:
            return parse_config(raw)
        return parse_config(
            {"plugins": {"entries": {"auto-routing": dict(raw)}}}
        )

    @staticmethod
    def _authority_document(proposal: AutoRoutingConfig) -> dict[str, Any]:
        return authority_document(proposal)

    @staticmethod
    def _config_document(proposal: AutoRoutingConfig) -> dict[str, Any]:
        return config_document(proposal)

    @classmethod
    def _baseline_revision(
        cls,
        proposal: AutoRoutingConfig,
        *,
        authority_id: str,
    ) -> AdaptiveRevision:
        document = cls._authority_document(proposal)
        revision_seed = _canonical_json({
            "kind": "stage1-initial-baseline",
            "authority_id": authority_id,
            "authority": document,
        })
        return AdaptiveRevision(
            revision_id=_checksum(revision_seed),
            authority_id=authority_id,
            parent_revision_id=None,
            overlay={},
            explanation={"kind": "baseline"},
            created_at=BASELINE_CREATED_AT,
            is_baseline=True,
        )

    def preview_config(self, proposal_path: str | Path) -> dict[str, Any]:
        self._assert_profile_isolation()
        proposal = self.load_proposal(proposal_path)
        config_preview = preview_update(proposal, path=self.config_path)
        authority_id = authority_revision(proposal)
        authority_document = self._authority_document(proposal)
        authority_json = _canonical_json(authority_document)
        baseline = self._baseline_revision(proposal, authority_id=authority_id)
        baseline_document = baseline.model_dump(mode="json", by_alias=True)
        baseline_json = _canonical_json(baseline_document)
        return {
            "applied": False,
            "operation": "preview",
            "activation": proposal.activation.model_dump(mode="json"),
            "config_path": str(config_preview.config_path),
            "before_sha256": config_preview.before_sha256,
            "after_sha256": config_preview.after_sha256,
            "precondition_sha256": config_preview.precondition_sha256,
            "expected_config_sha256": config_preview.precondition_sha256,
            "diff": config_preview.unified_diff,
            "authority_id": authority_id,
            "authority": {
                "document": authority_document,
                "canonical_json": authority_json,
                "checksum": _checksum(authority_json),
            },
            "initial_revision": {
                "document": baseline_document,
                "canonical_json": baseline_json,
                "checksum": _checksum(baseline_json),
            },
        }

    @staticmethod
    def _activation_proposal(
        current: AutoRoutingConfig,
        mode: str,
    ) -> AutoRoutingConfig:
        if mode not in {"shadow", "active"}:
            raise AutoRoutingServiceError(
                "activation mode must be shadow or active"
            )
        return current.model_copy(
            update={"activation": ActivationSettings(mode=mode)}
        )

    @staticmethod
    def _assert_activation_only(
        before_bytes: bytes,
        proposal: AutoRoutingConfig,
    ) -> None:
        try:
            current = parse_config(fast_safe_load(before_bytes))
        except Exception as error:
            raise AutoRoutingServiceError(
                "valid auto-routing authority is required"
            ) from error
        expected = current.model_copy(
            update={"activation": proposal.activation}
        )
        if config_document(expected) != config_document(proposal):
            raise ConfigConflict(
                "auto-routing authority changed; request and approve a new preview"
            )

    @staticmethod
    def _adapter_contract(adapter: Any) -> tuple[dict[str, Any], str]:
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
                raise AutoRoutingServiceError(
                    "runtime adapter capability contract is incompatible"
                )
            normalized = dict(report)
            payload = json.dumps(
                normalized,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        except AutoRoutingServiceError:
            raise
        except Exception as error:
            raise AutoRoutingServiceError(
                "runtime adapter capability contract is unavailable"
            ) from error
        return normalized, _checksum(payload)

    def _activation_inventory_fingerprint(
        self,
        proposal: AutoRoutingConfig,
    ) -> tuple[Any, str]:
        del proposal
        row = self.store.connection.execute(
            "SELECT snapshot_id FROM inventory_snapshots "
            "WHERE complete = 1 ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise AutoRoutingServiceError(
                "a complete persisted inventory snapshot is required"
            )
        snapshot = self.store.read_inventory_snapshot(str(row["snapshot_id"]))
        if snapshot is None:
            raise AutoRoutingServiceError(
                "the newest persisted inventory snapshot is unavailable"
            )
        return snapshot, snapshot.checksum

    def _assert_adapter_capability_unchanged(self, expected_sha: str) -> None:
        try:
            _report, current_sha = self._adapter_contract(self.adapter)
        except Exception as error:
            raise AutoRoutingServiceError(
                "runtime adapter capability changed after doctor approval"
            ) from error
        if not hmac.compare_digest(expected_sha, current_sha):
            raise AutoRoutingServiceError(
                "runtime adapter capability changed after doctor approval"
            )

    @staticmethod
    def _activation_precondition_sha(
        *,
        config_precondition_sha: str,
        proposal: AutoRoutingConfig,
        fingerprints: Mapping[str, Any],
    ) -> str:
        return _checksum(
            _canonical_json({
                "kind": "auto-routing-activation-v1",
                "config_precondition_sha": config_precondition_sha,
                "proposed_config_sha": config_revision(proposal),
                "authority_id": authority_revision(proposal),
                "inventory_revision": fingerprints.get("inventory_revision"),
                "inventory_contract_sha": fingerprints.get("inventory_contract_sha"),
                "adapter_capability_sha": fingerprints.get("adapter_capability_sha"),
            })
        )

    def preview_activation(self, mode: str = "active") -> dict[str, Any]:
        """Preview one explicit shadow/active transition without writing state."""
        self._assert_profile_isolation()
        current = self._configured_authority()
        proposal = self._activation_proposal(current, mode)
        with locked_update(
            proposal,
            path=self.config_path,
            allow_active=True,
        ) as mutation:
            self._assert_activation_only(mutation.preview.before_bytes, proposal)
            doctor = self.doctor(
                _proposal=proposal,
                _activation_transition=True,
            )
            if mode == "active" and not doctor["healthy"]:
                failures = [
                    item["name"]
                    for item in doctor["checks"]
                    if item["status"] == "error"
                ]
                raise AutoRoutingServiceError(
                    "active activation requires a healthy doctor: "
                    + ", ".join(failures)
                )
            preview = mutation.preview
            fingerprints = doctor.get("fingerprints", {})
            approved_precondition = self._activation_precondition_sha(
                config_precondition_sha=preview.precondition_sha256,
                proposal=proposal,
                fingerprints=fingerprints,
            )
            return {
                "applied": False,
                "operation": "activation_preview",
                "activation": proposal.activation.model_dump(mode="json"),
                "config_path": str(preview.config_path),
                "before_sha256": preview.before_sha256,
                "after_sha256": preview.after_sha256,
                "config_precondition_sha256": preview.precondition_sha256,
                "precondition_sha256": approved_precondition,
                "expected_config_sha256": approved_precondition,
                "proposed_config_sha256": config_revision(proposal),
                "authority_id": authority_revision(proposal),
                "diff": preview.unified_diff,
                "doctor": doctor,
                "fingerprints": fingerprints,
            }

    @staticmethod
    def _activation_receipt_from_journal(
        journal: Mapping[str, Any],
    ) -> ActivationReceipt | None:
        raw = journal.get("activation_receipt")
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise AutoRoutingServiceError(
                "pending activation receipt metadata is invalid"
            )
        try:
            return ActivationReceipt(**dict(raw))
        except Exception as error:
            raise AutoRoutingServiceError(
                "pending activation receipt metadata is invalid"
            ) from error

    def apply_activation(
        self,
        mode: str = "active",
        *,
        expected_config_sha256: str,
    ) -> dict[str, Any]:
        """Apply a doctor-approved activation through the recoverable saga."""
        self._assert_profile_isolation()
        if self._has_incomplete_config_apply():
            raise AutoRoutingServiceError(
                "an incomplete config apply must be recovered before another write"
            )
        current = self._configured_authority()
        proposal = self._activation_proposal(current, mode)
        operation_id = uuid.uuid4().hex
        backup_path = self.config_path.with_name(
            f"{self.config_path.name}.auto-routing.{operation_id}.bak"
        )
        journal_path = self.config_path.with_name(
            f"auto-routing-apply-{operation_id}.pending.json"
        )

        with locked_update(
            proposal,
            path=self.config_path,
            allow_active=True,
        ) as mutation, self._activation_write_transaction(journal_path):
            preview = mutation.preview
            self._assert_activation_only(preview.before_bytes, proposal)
            doctor = self.doctor(
                _proposal=proposal,
                _activation_transition=True,
            )
            if mode == "active" and not doctor["healthy"]:
                failures = [
                    item["name"]
                    for item in doctor["checks"]
                    if item["status"] == "error"
                ]
                raise AutoRoutingServiceError(
                    "active activation requires a healthy doctor: "
                    + ", ".join(failures)
                )

            fingerprints = doctor.get("fingerprints", {})
            approved_precondition = self._activation_precondition_sha(
                config_precondition_sha=preview.precondition_sha256,
                proposal=proposal,
                fingerprints=fingerprints,
            )
            if not isinstance(expected_config_sha256, str) or not hmac.compare_digest(
                expected_config_sha256,
                approved_precondition,
            ):
                raise ConfigConflict(
                    "activation precondition changed; request and approve a new preview"
                )

            config_sha = config_revision(proposal)
            authority_id = authority_revision(proposal)
            authority_value = self._authority_document(proposal)
            baseline = self._baseline_revision(
                proposal,
                authority_id=authority_id,
            )
            authority_json = _canonical_json(authority_value)
            baseline_json = _canonical_json(
                baseline.model_dump(mode="json", by_alias=True)
            )
            receipt: ActivationReceipt | None = None
            receipt_preexisting = False
            if mode == "active":
                inventory_sha = str(fingerprints.get("inventory_contract_sha") or "")
                inventory_revision = str(
                    fingerprints.get("inventory_revision") or ""
                )
                adapter_sha = str(fingerprints.get("adapter_capability_sha") or "")
                if not all((inventory_sha, inventory_revision, adapter_sha)):
                    raise AutoRoutingServiceError(
                        "doctor did not produce activation fingerprints"
                    )
                existing = self.store.read_matching_activation_receipt(
                    authority_id=authority_id,
                    config_sha=config_sha,
                    adapter_capability_sha=adapter_sha,
                    inventory_contract_sha=inventory_sha,
                    inventory_revision=inventory_revision,
                )
                if existing is not None:
                    receipt = existing
                    receipt_preexisting = True
                else:
                    receipt_seed = _canonical_json({
                        "authority_id": authority_id,
                        "config_sha": config_sha,
                        "inventory_contract_sha": inventory_sha,
                        "inventory_revision": inventory_revision,
                        "adapter_capability_sha": adapter_sha,
                    })
                    receipt = ActivationReceipt(
                        receipt_id=f"activation-{_checksum(receipt_seed)}",
                        authority_id=authority_id,
                        config_sha=config_sha,
                        inventory_contract_sha=inventory_sha,
                        inventory_revision=inventory_revision,
                        adapter_capability_sha=adapter_sha,
                        created_at=self._runtime_timestamp(),
                    )

            if mode == "active":
                self._assert_adapter_capability_unchanged(adapter_sha)

            if preview.before_bytes == preview.after_bytes:
                if mode == "active" and receipt is None:
                    raise AutoRoutingServiceError(
                        "active-mode configuration has no matching activation receipt; "
                        "transition to shadow before activating"
                    )
                if mode == "active" and not receipt_preexisting:
                    raise AutoRoutingServiceError(
                        "hand-edited active-mode configuration must return to shadow "
                        "before activation"
                    )
                if mode == "active" and not self._authority_is_usable(
                    proposal,
                    authority_id,
                ):
                    raise AutoRoutingServiceError(
                        "active no-op requires an exact usable authority baseline"
                    )
                return {
                    "applied": False,
                    "operation": "activation_noop",
                    "activation": proposal.activation.model_dump(mode="json"),
                    "config_path": str(preview.config_path),
                    "before_sha256": preview.before_sha256,
                    "after_sha256": preview.after_sha256,
                    "config_precondition_sha256": preview.precondition_sha256,
                    "precondition_sha256": approved_precondition,
                    "expected_config_sha256": approved_precondition,
                    "proposed_config_sha256": config_sha,
                    "authority_id": authority_id,
                    "doctor": doctor,
                    "fingerprints": fingerprints,
                }

            authority_before = self.store.read_authority_revision(authority_id)
            baseline_before = self.store.read_revision(baseline.revision_id)
            active_before = self.store.read_active_revision(authority_id)
            mutation.create_backup(backup_path)
            journal = {
                "version": 1,
                "operation_kind": "activation",
                "operation_id": operation_id,
                "phase": "prepared",
                "config_path": str(self.config_path),
                "backup_path": str(backup_path),
                "source_existed": mutation.source_existed,
                "before_sha256": preview.before_sha256,
                "after_sha256": preview.after_sha256,
                "config_noop": False,
                "activation_mode": mode,
                "activation_receipt": (
                    None if receipt is None else asdict(receipt)
                ),
                "receipt_preexisting": receipt_preexisting,
                "authority_id": authority_id,
                "authority_checksum": _checksum(authority_json),
                "baseline_revision_id": baseline.revision_id,
                "baseline_checksum": _checksum(baseline_json),
                "baseline_created_at": baseline.created_at,
                "authority_preexisting": authority_before is not None,
                "baseline_preexisting": baseline_before is not None,
                "active_pointer_preexisting": active_before is not None,
            }
            self._write_journal(journal_path, journal)
            self._inject("after_activation_prepared")
            created_authority = False
            created_baseline = False
            created_active_pointer = False
            try:
                if receipt is not None:
                    self._assert_adapter_capability_unchanged(adapter_sha)
                    self.store.write_activation_receipt(receipt)
                authority, revision = self.store.publish_authority_and_baseline(
                    authority_id=authority_id,
                    document=authority_value,
                    baseline=baseline,
                )
                created_authority = authority_before is None
                created_baseline = baseline_before is None
                created_active_pointer = active_before is None
                journal["phase"] = "receipt_published"
                self._write_journal(journal_path, journal)
                self._inject("after_receipt_before_yaml")
                mutation.replace()
                journal["phase"] = "yaml_replaced"
                self._write_journal(journal_path, journal)
                self._inject("after_yaml_before_saga_commit")
                if (
                    authority.checksum != _checksum(authority_json)
                    or revision != baseline
                    or self.store.read_active_revision(authority_id) != baseline
                ):
                    raise AutoRoutingServiceError(
                        "activation authority publication verification failed"
                    )
                if receipt is not None:
                    matching = self.store.read_activation_receipt(
                        receipt.receipt_id
                    )
                    if matching != receipt:
                        raise AutoRoutingServiceError(
                            "activation receipt publication verification failed"
                        )
                journal["phase"] = "complete"
                self._write_journal(journal_path, journal)
                self._inject("after_activation_complete")
            except Exception as original_error:
                try:
                    if created_authority or created_baseline or created_active_pointer:
                        self.store.rollback_authority_and_baseline(
                            authority_id=authority_id,
                            baseline_revision_id=baseline.revision_id,
                            remove_authority=created_authority,
                            remove_baseline=created_baseline,
                            remove_active_pointer=created_active_pointer,
                        )
                    mutation.restore(backup_path)
                except Exception as recovery_error:
                    self._incomplete_config_apply = True
                    self._recovery_error = str(recovery_error)
                    raise AutoRoutingServiceError(
                        "activation failed and recovery is incomplete"
                    ) from original_error
                self._remove_journal(journal_path)
                raise
        return {
            "applied": True,
            "operation": "activation_apply",
            "activation": proposal.activation.model_dump(mode="json"),
            "config_path": str(preview.config_path),
            "before_sha256": preview.before_sha256,
            "after_sha256": preview.after_sha256,
            "config_precondition_sha256": preview.precondition_sha256,
            "precondition_sha256": approved_precondition,
            "expected_config_sha256": approved_precondition,
            "proposed_config_sha256": config_sha,
            "authority_id": authority_id,
            "activation_receipt_id": (
                None if receipt is None else receipt.receipt_id
            ),
            "backup_path": str(backup_path),
            "doctor": doctor,
            "fingerprints": fingerprints,
        }

    def apply_config(
        self,
        proposal_path: str | Path,
        *,
        expected_config_sha256: str,
    ) -> dict[str, Any]:
        self._assert_profile_isolation()
        if self._has_incomplete_config_apply():
            raise AutoRoutingServiceError(
                "an incomplete config apply must be recovered before another write"
            )
        proposal = self.load_proposal(proposal_path)
        approved_preview = self.preview_config(proposal_path)
        authority_id = authority_revision(proposal)
        authority_document = self._authority_document(proposal)
        baseline = self._baseline_revision(proposal, authority_id=authority_id)
        authority_json = _canonical_json(authority_document)
        baseline_json = _canonical_json(
            baseline.model_dump(mode="json", by_alias=True)
        )
        operation_id = uuid.uuid4().hex
        backup_path = self.config_path.with_name(
            f"{self.config_path.name}.auto-routing.{operation_id}.bak"
        )
        journal_path = self.config_path.with_name(
            f"auto-routing-apply-{operation_id}.pending.json"
        )

        with locked_update(proposal, path=self.config_path) as mutation:
            preview = mutation.preview
            if not isinstance(expected_config_sha256, str) or not hmac.compare_digest(
                expected_config_sha256,
                preview.precondition_sha256,
            ):
                raise ConfigConflict(
                    "config apply precondition changed; request and approve a new preview"
                )
            authority_before = self.store.read_authority_revision(authority_id)
            baseline_before = self.store.read_revision(baseline.revision_id)
            active_before = self.store.read_active_revision(authority_id)
            mutation.create_backup(backup_path)
            journal = {
                "version": 1,
                "operation_id": operation_id,
                "phase": "prepared",
                "config_path": str(self.config_path),
                "backup_path": str(backup_path),
                "source_existed": mutation.source_existed,
                "before_sha256": preview.before_sha256,
                "after_sha256": preview.after_sha256,
                "config_noop": preview.before_bytes == preview.after_bytes,
                "authority_id": authority_id,
                "authority_checksum": _checksum(authority_json),
                "baseline_revision_id": baseline.revision_id,
                "baseline_checksum": _checksum(baseline_json),
                "baseline_created_at": baseline.created_at,
                "authority_preexisting": authority_before is not None,
                "baseline_preexisting": baseline_before is not None,
                "active_pointer_preexisting": active_before is not None,
            }
            self._write_journal(journal_path, journal)
            self._inject("after_apply_prepared")
            created_authority = False
            created_baseline = False
            created_active_pointer = False
            try:
                mutation.replace()
                journal["phase"] = "yaml_replaced"
                self._write_journal(journal_path, journal)
                self._inject("after_yaml_replace")
                self._inject("before_baseline_publish")
                authority, revision = self.store.publish_authority_and_baseline(
                    authority_id=authority_id,
                    document=authority_document,
                    baseline=baseline,
                )
                created_authority = authority_before is None
                created_baseline = baseline_before is None
                created_active_pointer = active_before is None
                journal["phase"] = "db_published"
                self._write_journal(journal_path, journal)
                self._inject("after_baseline_publish")
                if (
                    authority.checksum != _checksum(authority_json)
                    or revision != baseline
                    or self.store.read_active_revision(authority_id) != baseline
                ):
                    raise AutoRoutingServiceError(
                        "authority publication verification failed"
                    )
                journal["phase"] = "complete"
                self._write_journal(journal_path, journal)
                self._inject("after_apply_complete")
                self._remove_journal(journal_path)
            except Exception as original_error:
                try:
                    if created_authority or created_baseline or created_active_pointer:
                        self.store.rollback_authority_and_baseline(
                            authority_id=authority_id,
                            baseline_revision_id=baseline.revision_id,
                            remove_authority=created_authority,
                            remove_baseline=created_baseline,
                            remove_active_pointer=created_active_pointer,
                        )
                    mutation.restore(backup_path)
                except Exception as recovery_error:
                    self._incomplete_config_apply = True
                    self._recovery_error = str(recovery_error)
                    raise AutoRoutingServiceError(
                        "config apply failed and recovery is incomplete"
                    ) from original_error
                self._remove_journal(journal_path)
                raise
        return {
            **approved_preview,
            "applied": True,
            "operation": "apply",
            "backup_path": str(backup_path),
            "authority_id": authority_id,
        }

    def _inject(self, name: str) -> None:
        callback = getattr(self._fault_injector, name, None)
        if callable(callback):
            callback()

    @contextmanager
    def _activation_write_transaction(self, journal_path: Path):
        """Commit durable activation state before removing its recovery journal."""
        with self.store.write_txn():
            yield
        if journal_path.exists():
            self._inject("after_activation_db_commit_before_journal_remove")
            self._remove_journal(journal_path)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name != "posix":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def _write_journal(cls, path: Path, journal: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (_canonical_json(journal) + "\n").encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            cls._fsync_directory(path.parent)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    @classmethod
    def _remove_journal(cls, path: Path) -> None:
        path.unlink(missing_ok=True)
        cls._fsync_directory(path.parent)

    def _recover_pending_applies(self) -> None:
        try:
            self._assert_profile_isolation()
        except Exception as error:
            self._incomplete_config_apply = True
            self._recovery_error = str(error)
            return
        pending = self._pending_apply_journals()
        if not pending:
            return
        try:
            for journal_path in pending:
                self._recover_journal(journal_path)
        except Exception as error:
            self._incomplete_config_apply = True
            self._recovery_error = str(error) or type(error).__name__

    def _pending_apply_journals(self) -> list[Path]:
        return sorted(
            self.config_path.parent.glob("auto-routing-apply-*.pending.json")
        )

    def _has_incomplete_config_apply(self) -> bool:
        return self._incomplete_config_apply or bool(
            self._pending_apply_journals()
        )

    def _recover_journal(self, journal_path: Path) -> None:
        with profile_config_lock(self.config_path):
            if not journal_path.exists():
                return
            self._recover_journal_locked(journal_path)

    def _recover_journal_locked(self, journal_path: Path) -> None:
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except Exception as error:
            raise AutoRoutingServiceError("pending apply journal is unreadable") from error
        if not isinstance(journal, dict) or journal.get("version") != 1:
            raise AutoRoutingServiceError("pending apply journal schema is invalid")
        if journal.get("operation_kind") == "activation":
            self._recover_activation_journal_locked(journal_path, journal)
            return
        if journal.get("phase") not in {
            "prepared",
            "yaml_replaced",
            "db_published",
            "complete",
        }:
            raise AutoRoutingServiceError("pending apply journal phase is invalid")
        if not isinstance(journal.get("source_existed"), bool):
            raise AutoRoutingServiceError("pending apply source state is invalid")
        config_noop = journal.get("config_noop", False)
        if not isinstance(config_noop, bool):
            raise AutoRoutingServiceError("pending apply no-op state is invalid")
        operation_id = str(journal.get("operation_id") or "")
        expected_name = f"auto-routing-apply-{operation_id}.pending.json"
        if not operation_id or journal_path.name != expected_name:
            raise AutoRoutingServiceError("pending apply journal identity is invalid")
        if Path(str(journal.get("config_path"))) != self.config_path:
            raise AutoRoutingServiceError("pending apply belongs to another profile")
        backup_path = Path(str(journal.get("backup_path")))
        if (
            backup_path.parent != self.config_path.parent
            or backup_path.name
            != f"{self.config_path.name}.auto-routing.{operation_id}.bak"
        ):
            raise AutoRoutingServiceError("pending apply backup path is invalid")
        if not backup_path.is_file():
            raise AutoRoutingServiceError("pending apply backup is missing")
        before_bytes = backup_path.read_bytes()
        before_hash = hashlib.sha256(before_bytes).hexdigest()
        if before_hash != journal.get("before_sha256"):
            raise AutoRoutingServiceError("pending apply backup checksum changed")
        current_exists = self.config_path.exists()
        current_bytes = self.config_path.read_bytes() if current_exists else None
        current_hash = (
            None
            if current_bytes is None
            else hashlib.sha256(current_bytes).hexdigest()
        )
        before_state_matches = (
            current_exists is journal["source_existed"]
            and (
                not current_exists
                or current_hash == journal.get("before_sha256")
            )
        )
        after_state_matches = (
            current_exists and current_hash == journal.get("after_sha256")
        )
        if config_noop:
            if (
                not journal["source_existed"]
                or journal.get("before_sha256") != journal.get("after_sha256")
                or current_bytes != before_bytes
            ):
                raise AutoRoutingServiceError(
                    "pending no-op config changed outside the apply"
                )
            self._recover_noop_journal(
                journal_path,
                journal,
                current_bytes=before_bytes,
            )
            return
        if before_state_matches and after_state_matches:
            raise AutoRoutingServiceError(
                "pending apply config state is ambiguous"
            )
        if before_state_matches:
            self._rollback_prepared_database(journal)
            self._remove_journal(journal_path)
            return
        if not after_state_matches:
            raise AutoRoutingServiceError(
                "config changed outside the pending apply; recovery is fail-closed"
            )
        assert current_bytes is not None
        self._publish_pending_config(journal, current_bytes=current_bytes)
        self._remove_journal(journal_path)

    def _recover_activation_journal_locked(
        self,
        journal_path: Path,
        journal: Mapping[str, Any],
    ) -> None:
        if journal.get("phase") not in {
            "prepared",
            "receipt_published",
            "yaml_replaced",
            "complete",
        }:
            raise AutoRoutingServiceError(
                "pending activation journal phase is invalid"
            )
        if journal.get("activation_mode") not in {"shadow", "active"}:
            raise AutoRoutingServiceError(
                "pending activation mode is invalid"
            )
        if not isinstance(journal.get("source_existed"), bool):
            raise AutoRoutingServiceError(
                "pending activation source state is invalid"
            )
        if journal.get("config_noop") is not False:
            raise AutoRoutingServiceError(
                "pending activation no-op state is invalid"
            )
        operation_id = str(journal.get("operation_id") or "")
        if (
            not operation_id
            or journal_path.name
            != f"auto-routing-apply-{operation_id}.pending.json"
        ):
            raise AutoRoutingServiceError(
                "pending activation journal identity is invalid"
            )
        if Path(str(journal.get("config_path"))) != self.config_path:
            raise AutoRoutingServiceError(
                "pending activation belongs to another profile"
            )
        backup_path = Path(str(journal.get("backup_path")))
        if (
            backup_path.parent != self.config_path.parent
            or backup_path.name
            != f"{self.config_path.name}.auto-routing.{operation_id}.bak"
            or not backup_path.is_file()
        ):
            raise AutoRoutingServiceError(
                "pending activation backup is invalid"
            )
        before_bytes = backup_path.read_bytes()
        before_sha = hashlib.sha256(before_bytes).hexdigest()
        if before_sha != journal.get("before_sha256"):
            raise AutoRoutingServiceError(
                "pending activation backup checksum changed"
            )
        if not self.config_path.exists():
            current_bytes = None
            current_sha = None
        else:
            current_bytes = self.config_path.read_bytes()
            current_sha = hashlib.sha256(current_bytes).hexdigest()
        before_matches = (
            (current_bytes is not None) is journal["source_existed"]
            and (
                current_bytes is None
                or current_sha == journal.get("before_sha256")
            )
        )
        after_matches = (
            current_bytes is not None
            and current_sha == journal.get("after_sha256")
        )
        if before_matches and after_matches:
            raise AutoRoutingServiceError(
                "pending activation config state is ambiguous"
            )
        if before_matches:
            # The activation transaction did not commit if the old YAML is
            # still authoritative. Remove any pre-transaction authority state
            # described by the journal and discard the attempt.
            self._rollback_prepared_database(journal)
            self._remove_journal(journal_path)
            return
        if not after_matches or current_bytes is None:
            raise AutoRoutingServiceError(
                "config changed outside the pending activation; recovery is fail-closed"
            )

        receipt = self._activation_receipt_from_journal(journal)
        active = journal["activation_mode"] == "active"
        if active and receipt is None:
            raise AutoRoutingServiceError("pending active activation has no receipt")
        if not active and receipt is not None:
            raise AutoRoutingServiceError(
                "pending shadow activation unexpectedly has a receipt"
            )
        if receipt is not None:
            try:
                proposal = parse_config(fast_safe_load(current_bytes))
            except Exception as error:
                raise AutoRoutingServiceError(
                    "pending active activation config is invalid"
                ) from error
            if (
                receipt.authority_id != authority_revision(proposal)
                or receipt.config_sha != config_revision(proposal)
            ):
                raise AutoRoutingServiceError(
                    "pending active activation receipt does not match config"
                )
        with self.store.write_txn():
            if receipt is not None:
                self.store.write_activation_receipt(receipt)
            self._publish_pending_config(journal, current_bytes=current_bytes)
            if receipt is not None:
                matching = self.store.read_activation_receipt(
                    receipt.receipt_id
                )
                if matching != receipt:
                    raise AutoRoutingServiceError(
                        "pending active activation receipt is unavailable"
                    )
        self._remove_journal(journal_path)

    def _pending_apply_records(
        self,
        journal: Mapping[str, Any],
        *,
        current_bytes: bytes,
    ) -> tuple[str, dict[str, Any], AdaptiveRevision]:
        try:
            proposal = parse_config(fast_safe_load(current_bytes))
        except Exception as error:
            raise AutoRoutingServiceError("pending applied config is invalid") from error
        authority_id = authority_revision(proposal)
        authority_document = self._authority_document(proposal)
        baseline = self._baseline_revision(proposal, authority_id=authority_id)
        authority_json = _canonical_json(authority_document)
        baseline_json = _canonical_json(
            baseline.model_dump(mode="json", by_alias=True)
        )
        expected = {
            "authority_id": authority_id,
            "authority_checksum": _checksum(authority_json),
            "baseline_revision_id": baseline.revision_id,
            "baseline_checksum": _checksum(baseline_json),
            "baseline_created_at": baseline.created_at,
        }
        if any(journal.get(key) != value for key, value in expected.items()):
            raise AutoRoutingServiceError("pending apply authority metadata changed")
        return authority_id, authority_document, baseline

    def _publish_pending_config(
        self,
        journal: Mapping[str, Any],
        *,
        current_bytes: bytes,
    ) -> None:
        authority_id, authority_document, baseline = self._pending_apply_records(
            journal,
            current_bytes=current_bytes,
        )
        self.store.publish_authority_and_baseline(
            authority_id=authority_id,
            document=authority_document,
            baseline=baseline,
        )
        if self.store.read_active_revision(authority_id) != baseline:
            raise AutoRoutingServiceError("pending apply baseline verification failed")

    def _journal_database_state(
        self,
        journal: Mapping[str, Any],
    ) -> tuple[bool, bool]:
        authority_id = str(journal.get("authority_id") or "")
        baseline_id = str(journal.get("baseline_revision_id") or "")
        authority_checksum = str(journal.get("authority_checksum") or "")
        baseline_checksum = str(journal.get("baseline_checksum") or "")
        if not all(
            len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
            for value in (
                authority_id,
                baseline_id,
                authority_checksum,
                baseline_checksum,
            )
        ):
            raise AutoRoutingServiceError(
                "pending apply database identity metadata is invalid"
            )
        ownership = self._journal_database_ownership(journal)
        if ownership is None:
            raise AutoRoutingServiceError(
                "pending no-op database ownership metadata is missing"
            )
        authority = self.store.read_authority_revision(authority_id)
        baseline = self.store.read_revision(baseline_id)
        active = self.store.read_active_revision(authority_id)
        if authority is not None and authority.checksum != authority_checksum:
            raise AutoRoutingServiceError(
                "pending apply authority database content changed"
            )
        if baseline is not None:
            actual_baseline_checksum = _checksum(
                _canonical_json(
                    baseline.model_dump(mode="json", by_alias=True)
                )
            )
            if (
                baseline.authority_id != authority_id
                or not baseline.is_baseline
                or actual_baseline_checksum != baseline_checksum
            ):
                raise AutoRoutingServiceError(
                    "pending apply baseline database content changed"
                )
        if active is not None and active.revision_id != baseline_id:
            raise AutoRoutingServiceError(
                "pending apply active database pointer changed"
            )
        observed = (authority is not None, baseline is not None, active is not None)
        before_matches = observed == ownership
        after_matches = observed == (True, True, True)
        if not before_matches and not after_matches:
            raise AutoRoutingServiceError(
                "pending no-op database state changed outside the apply"
            )
        return before_matches, after_matches

    def _recover_noop_journal(
        self,
        journal_path: Path,
        journal: Mapping[str, Any],
        *,
        current_bytes: bytes,
    ) -> None:
        phase = str(journal["phase"])
        _before_matches, after_matches = self._journal_database_state(journal)
        if phase == "prepared":
            self._rollback_prepared_database(journal)
        elif phase == "yaml_replaced":
            self._publish_pending_config(journal, current_bytes=current_bytes)
        elif phase in {"db_published", "complete"}:
            self._pending_apply_records(journal, current_bytes=current_bytes)
            if not after_matches:
                raise AutoRoutingServiceError(
                    "pending no-op published database state changed"
                )
        else:  # pragma: no cover - validated by the caller
            raise AutoRoutingServiceError("pending apply journal phase is invalid")
        self._remove_journal(journal_path)

    @staticmethod
    def _journal_database_ownership(
        journal: Mapping[str, Any],
    ) -> tuple[bool, bool, bool] | None:
        names = (
            "authority_preexisting",
            "baseline_preexisting",
            "active_pointer_preexisting",
        )
        values = tuple(journal.get(name) for name in names)
        if values == (None, None, None):
            return None
        if any(not isinstance(value, bool) for value in values):
            raise AutoRoutingServiceError(
                "pending apply database ownership metadata is invalid"
            )
        authority_preexisting, baseline_preexisting, active_preexisting = values
        if baseline_preexisting and not authority_preexisting:
            raise AutoRoutingServiceError(
                "pending apply baseline ownership metadata is inconsistent"
            )
        if active_preexisting and not baseline_preexisting:
            raise AutoRoutingServiceError(
                "pending apply active-pointer ownership metadata is inconsistent"
            )
        return (
            authority_preexisting,
            baseline_preexisting,
            active_preexisting,
        )

    def _rollback_prepared_database(self, journal: Mapping[str, Any]) -> None:
        authority_id = str(journal.get("authority_id") or "")
        baseline_id = str(journal.get("baseline_revision_id") or "")
        authority_checksum = str(journal.get("authority_checksum") or "")
        baseline_checksum = str(journal.get("baseline_checksum") or "")
        if not all(
            len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
            for value in (
                authority_id,
                baseline_id,
                authority_checksum,
                baseline_checksum,
            )
        ):
            raise AutoRoutingServiceError(
                "pending apply database identity metadata is invalid"
            )
        authority = self.store.read_authority_revision(authority_id)
        baseline = self.store.read_revision(baseline_id)
        active = self.store.read_active_revision(authority_id)
        if authority is not None and authority.checksum != authority_checksum:
            raise AutoRoutingServiceError(
                "pending apply authority database content changed"
            )
        if baseline is not None:
            actual_baseline_checksum = _checksum(
                _canonical_json(
                    baseline.model_dump(mode="json", by_alias=True)
                )
            )
            if (
                baseline.authority_id != authority_id
                or not baseline.is_baseline
                or actual_baseline_checksum != baseline_checksum
            ):
                raise AutoRoutingServiceError(
                    "pending apply baseline database content changed"
                )
        if active is not None and active.revision_id != baseline_id:
            raise AutoRoutingServiceError(
                "pending apply active database pointer changed"
            )
        ownership = self._journal_database_ownership(journal)
        if ownership is None:
            if any(value is not None for value in (authority, baseline, active)):
                raise AutoRoutingServiceError(
                    "pending apply database ownership metadata is missing"
                )
            return
        authority_preexisting, baseline_preexisting, active_preexisting = ownership
        observed = (authority is not None, baseline is not None, active is not None)
        for was_preexisting, is_present in zip(
            ownership,
            observed,
            strict=True,
        ):
            if was_preexisting and not is_present:
                raise AutoRoutingServiceError(
                    "pending apply preexisting database state changed"
                )
        self.store.rollback_authority_and_baseline(
            authority_id=authority_id,
            baseline_revision_id=baseline_id,
            remove_authority=not authority_preexisting,
            remove_baseline=not baseline_preexisting,
            remove_active_pointer=not active_preexisting,
        )

    def validate(self, proposal_path: str | Path | None = None) -> dict[str, Any]:
        self._assert_profile_isolation()
        if proposal_path is None:
            try:
                root = fast_safe_load(self.config_path.read_bytes())
            except Exception as error:
                raise AutoRoutingServiceError("config could not be read") from error
            proposal = parse_config(root)
        else:
            proposal = self.load_proposal(proposal_path)
        return {
            "valid": True,
            "proposal": self._config_document(proposal),
            "authority_id": authority_revision(proposal),
            "activation": proposal.activation.model_dump(mode="json"),
        }

    def _configured_authority(self) -> AutoRoutingConfig:
        try:
            return parse_config(fast_safe_load(self.config_path.read_bytes()))
        except Exception as error:
            raise AutoRoutingServiceError(
                "valid auto-routing authority is required"
            ) from error

    def _current_persisted_inventory_snapshot(self) -> InventorySnapshot:
        """Load the one newest complete stored snapshot without refreshing it."""
        rows = self.store.connection.execute(
            "SELECT snapshot_id, created_at FROM inventory_snapshots "
            "WHERE complete=1 ORDER BY created_at DESC, rowid DESC LIMIT 2"
        ).fetchall()
        if not rows:
            raise _ManagementHoldError("inventory_snapshot_missing")
        if len(rows) > 1 and str(rows[0]["created_at"]) == str(rows[1]["created_at"]):
            raise _ManagementHoldError("inventory_snapshot_ambiguous")
        snapshot_id = str(rows[0]["snapshot_id"])
        stored = self.store.read_inventory_snapshot(snapshot_id)
        if stored is None:
            raise _ManagementHoldError("inventory_snapshot_unavailable")
        runtimes: list[ExecutableRuntime] = []
        for observation in stored.observations:
            if observation.key.inventory_revision != snapshot_id:
                raise _ManagementHoldError("inventory_snapshot_ambiguous")
            support = resolve_reasoning_support(
                provider=observation.key.provider,
                model=observation.key.model,
                api_mode=observation.key.api_mode,
                metadata=observation.capabilities,
            )
            runtimes.append(
                ExecutableRuntime(
                    key=observation.key,
                    resolver_name=observation.key.provider,
                    state=observation.state,
                    reasons=ReasonCodes(observation.reasons),
                    economics=observation.economics,
                    reasoning_support=support,
                    verification_source=observation.verification_source,
                    verified_at=observation.verified_at,
                    verification_expires_at=observation.verification_expires_at,
                    provenance=observation.provenance,
                    observed_at=observation.observed_at,
                    capabilities=observation.capabilities,
                )
            )
        runtime_ids = tuple(runtime.key.stable_id() for runtime in runtimes)
        if len(runtime_ids) != len(set(runtime_ids)):
            raise _ManagementHoldError("inventory_snapshot_ambiguous")
        return InventorySnapshot(
            revision=snapshot_id,
            runtimes=runtimes,
            observed_at=stored.created_at,
        )

    def _management_inventory_fingerprint(self, snapshot_id: str) -> str:
        stored = self.store.read_inventory_snapshot(snapshot_id)
        if stored is None:
            raise _ManagementHoldError("inventory_snapshot_unavailable")
        return stored.checksum

    def management_inventory(
        self,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Return only persisted runtime identities and eligibility counts."""
        moment = _management_utc_now(now)
        try:
            snapshot = self._current_persisted_inventory_snapshot()
            fingerprint = self._management_inventory_fingerprint(snapshot.revision)
        except _ManagementHoldError as error:
            return {
                "status": "unavailable",
                "reason_code": error.reason_code,
                "inventory_revision": None,
                "inventory_fingerprint": None,
                "observed_at": None,
                "runtime_count": 0,
                "eligible_count": 0,
                "eligible_runtime_ids": [],
                "rejection_reason_counts": {},
            }
        eligible = verified_inventory_candidates(snapshot, moment)
        rejection_counts: dict[str, int] = {}
        for runtime in snapshot.runtimes:
            for reason in management_inventory_ineligibility_reasons(runtime, moment):
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
        return {
            "status": "available",
            "reason_code": None,
            "inventory_revision": snapshot.revision,
            "inventory_fingerprint": fingerprint,
            "observed_at": snapshot.observed_at,
            "runtime_count": len(snapshot.runtimes),
            "eligible_count": len(eligible),
            "eligible_runtime_ids": [item.runtime_id for item in eligible],
            "rejection_reason_counts": dict(sorted(rejection_counts.items())),
        }

    @staticmethod
    def _managed_job_fingerprint(job: Mapping[str, Any]) -> str | None:
        """Return an exact stable identity for one managed script job.

        Generic cron owns transient run state and the final finite-job deletion,
        so neither is part of this binding.  The schedule and repeat counter are
        included to reject an old script process after a same-ID reschedule.
        """
        schedule = job.get("schedule")
        repeat = job.get("repeat")
        if not isinstance(schedule, Mapping) or not isinstance(repeat, Mapping):
            return None
        times = repeat.get("times")
        completed = repeat.get("completed")
        if not (
            str(job.get("id") or "")
            and job.get("name") == MANAGEMENT_CRON_NAME
            and job.get("script") == MANAGEMENT_SCRIPT_NAME
            and job.get("prompt") == ""
            and job.get("deliver") == "local"
            and job.get("no_agent") is True
            and job.get("script_launch_claim") is True
            and schedule.get("kind") in {"once", "interval", "cron"}
            and (times is None or times == 1)
            and isinstance(completed, int)
            and not isinstance(completed, bool)
        ):
            return None
        document = {
            key: value
            for key, value in job.items()
            if key not in {
                "_script_launch_capability",
                "_script_launch_capabilities",
                # Generic cron heartbeats this scheduler-owned lease while the
                # synchronous child is running. Its stable owner is enforced
                # by cron; it is not part of the management job definition.
                "run_claim",
            }
        }
        return _checksum(_canonical_json(document))

    @classmethod
    def _managed_one_shot_job_fingerprint(
        cls,
        job: Mapping[str, Any],
    ) -> str | None:
        """Return the managed fingerprint only when this is a finite run."""
        schedule = job.get("schedule")
        repeat = job.get("repeat")
        if not isinstance(schedule, Mapping) or not isinstance(repeat, Mapping):
            return None
        if schedule.get("kind") != "once" or repeat.get("times") != 1:
            return None
        return cls._managed_job_fingerprint(job)

    def _scheduled_management_invocation_matches(
        self,
        invocation: _ScheduledManagementInvocation,
        *,
        config: AutoRoutingConfig | None = None,
    ) -> bool:
        """Revalidate one asserted scheduler binding without side effects."""
        if not isinstance(invocation, _ScheduledManagementInvocation):
            return False
        try:
            current = self._configured_authority() if config is None else config
            with locked_cron_store(self.hermes_home):
                return self._scheduled_management_invocation_matches_locked(
                    invocation,
                    config=current,
                )
        except Exception:
            return False

    def _scheduled_management_invocation_matches_locked(
        self,
        invocation: _ScheduledManagementInvocation,
        *,
        config: AutoRoutingConfig,
    ) -> bool:
        """Match an invocation while the caller holds the profile cron lock."""
        authority_id = authority_revision(config)
        management_authority_id = management_authority_revision(config)
        if not hmac.compare_digest(authority_id, invocation.authority_id):
            return False
        if not hmac.compare_digest(
            management_authority_id,
            invocation.management_authority_id,
        ):
            return False
        control = self.store.read_management_control(management_authority_id)
        if (
            control.generation != invocation.management_control_generation
            or control.cron_job_id != invocation.cron_job_id
        ):
            return False
        job = get_job(invocation.cron_job_id)
        if job is None:
            return False
        fingerprint = self._managed_job_fingerprint(job)
        return fingerprint is not None and hmac.compare_digest(
            fingerprint,
            invocation.cron_job_fingerprint,
        )

    @contextmanager
    def _scheduled_management_mutation_guard(
        self,
        invocation: _ScheduledManagementInvocation,
        *,
        config: AutoRoutingConfig,
    ):
        """Hold the exact cron binding across the config mutation boundary."""
        stack = ExitStack()
        try:
            stack.enter_context(locked_cron_store_strict(self.hermes_home))
        except Exception:
            yield "scheduled_invocation_changed"
            return
        with stack:
            reason = None
            try:
                if not self._scheduled_management_invocation_matches_locked(
                    invocation,
                    config=config,
                ):
                    reason = "scheduled_invocation_changed"
            except Exception:
                reason = "scheduled_invocation_changed"
            yield reason

    def assert_scheduled_management_invocation(self) -> _ScheduledManagementInvocation:
        """Authorize one scheduler claim and bind its current managed job."""
        config = self._configured_authority()
        management_authority_id = management_authority_revision(config)
        control = self.store.read_management_control(
            management_authority_id
        )
        if not control.cron_job_id:
            raise AutoRoutingServiceError("scheduled management cron job is unavailable")
        with use_cron_store(self.hermes_home):
            job = get_job(control.cron_job_id)
        fingerprint = None if job is None else self._managed_job_fingerprint(job)
        one_shot_fingerprint = (
            None if job is None else self._managed_one_shot_job_fingerprint(job)
        )
        assert_management_scheduled_invocation(
            home=self.hermes_home,
            expected_job_id=control.cron_job_id,
        )
        if fingerprint is None:
            raise RuntimeError("scheduled invocation claim is invalid")
        invocation = _ScheduledManagementInvocation(
            authority_id=authority_revision(config),
            management_authority_id=management_authority_id,
            management_control_generation=control.generation,
            cron_job_id=control.cron_job_id,
            cron_job_fingerprint=fingerprint,
            one_shot_job_fingerprint=one_shot_fingerprint,
        )
        if not self._scheduled_management_invocation_matches(invocation):
            raise RuntimeError("scheduled invocation claim is invalid")
        return invocation

    def complete_scheduled_management_invocation(
        self,
        invocation: _ScheduledManagementInvocation,
    ) -> bool:
        """Clear only the one-shot control binding just completed successfully.

        This is intentionally invoked after reconciliation returns.  A failed or
        unauthorized script never reaches it, while generic cron remains solely
        responsible for deleting the finite job after the script exits.
        """
        if (
            not isinstance(invocation, _ScheduledManagementInvocation)
            or invocation.one_shot_job_fingerprint is None
        ):
            return False
        with profile_config_lock(self.config_path):
            config = self._configured_authority()
            management_authority_id = management_authority_revision(config)
            if management_authority_id != invocation.management_authority_id:
                return False
            control = self.store.read_management_control(management_authority_id)
            if (
                control.generation != invocation.management_control_generation
                or control.cron_job_id != invocation.cron_job_id
            ):
                return False
            with locked_cron_store(self.hermes_home):
                job = get_job(invocation.cron_job_id)
                fingerprint = (
                    None
                    if job is None
                    else self._managed_one_shot_job_fingerprint(job)
                )
                # Re-read inside the same jobs.json transaction. Besides
                # defending against in-process re-entrant updates, this gives
                # the transition an explicit stable snapshot while the cron
                # lock excludes competing CRUD writers.
                confirmed = get_job(invocation.cron_job_id)
                if (
                    fingerprint != invocation.one_shot_job_fingerprint
                    or confirmed is None
                    or self._managed_one_shot_job_fingerprint(confirmed)
                    != fingerprint
                ):
                    return False
                self._transition_global_management_control(
                    config=config,
                    current=control,
                    frozen=control.frozen,
                    cron_job_id=None,
                    action="one_shot_complete",
                    moment=_management_utc_now(None),
                )
        return True

    def management_ranking_status(
        self,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Verify the configured local pack without returning ranking rows."""
        moment = _management_utc_now(now)
        try:
            config = self._configured_authority()
        except AutoRoutingServiceError:
            return {
                "status": "unavailable",
                "reason_code": "management_config_invalid",
            }
        trust = config.autonomous_profile_management.ranking_pack
        if trust is None:
            return {
                "status": "unconfigured",
                "reason_code": "ranking_pack_trust_missing",
            }
        try:
            return dict(
                ranking_pack_status(
                    home=self.hermes_home,
                    trust=trust,
                    now=moment,
                )
            )
        except Exception:
            return {
                "status": "unavailable",
                "reason_code": "ranking_pack_unavailable",
            }

    def management_status(
        self,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Return the current global and per-profile management control state."""
        moment = _management_utc_now(now)
        config = self._configured_authority()
        settings = config.autonomous_profile_management
        management_authority_id = management_authority_revision(config)
        control = self.store.read_management_control(management_authority_id)
        utc_day = moment.date().isoformat()
        profiles: list[dict[str, Any]] = []
        for profile_id in sorted(config.profiles):
            state = self.store.read_management_profile_state(
                management_authority_id,
                profile_id,
                current_authority_id=authority_revision(config),
            )
            admitted = self.store.management_daily_admissions(profile_id, utc_day)
            profiles.append({
                "profile_id": profile_id,
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
                "changes_today": admitted,
                "remaining_changes_today": max(
                    0, settings.daily_change_limit - admitted
                ),
            })
        return {
            "enabled": settings.enabled,
            "frozen": control.frozen,
            "authority_id": authority_revision(config),
            "management_authority_id": management_authority_id,
            "control_generation": control.generation,
            "cron_job_id": control.cron_job_id,
            "schedule": settings.schedule,
            "daily_change_limit": settings.daily_change_limit,
            "updated_at": control.updated_at,
            "utc_day": utc_day,
            "profile_count": len(profiles),
            "profiles": profiles,
        }

    def management_history(
        self,
        *,
        profile_id: str | None = None,
    ) -> dict[str, Any]:
        """Return immutable lineage metadata without ranking rows or content."""
        config = self._configured_authority()
        if profile_id is not None and profile_id not in config.profiles:
            raise AutoRoutingServiceError(
                f"unknown management profile: {profile_id!r}"
            )
        management_authority_id = management_authority_revision(config)
        revision_rows = self.store.connection.execute(
            "SELECT revision_id FROM management_revisions "
            "WHERE management_authority_id=? "
            "ORDER BY management_epoch, created_at, revision_id",
            (management_authority_id,),
        ).fetchall()
        revisions: list[dict[str, Any]] = []
        revision_ids: list[str] = []
        for row in revision_rows:
            revision = self.store.read_management_revision(str(row["revision_id"]))
            if revision is None:
                raise AutoRoutingServiceError("management history is unavailable")
            patches = tuple(
                patch
                for patch in revision.patches
                if profile_id is None or patch.profile_id == profile_id
            )
            if not patches:
                continue
            revision_ids.append(revision.revision_id)
            revisions.append({
                "revision_id": revision.revision_id,
                "parent_revision_id": revision.parent_revision_id,
                "preceding_authority_id": revision.preceding_authority_id,
                "resulting_authority_id": revision.resulting_authority_id,
                "management_epoch": revision.management_epoch,
                "action": revision.action,
                "profile_ids": [patch.profile_id for patch in patches],
                "reason_codes": sorted({
                    reason for patch in patches for reason in patch.reason_codes
                }),
                "ranking_pack_id": revision.ranking_pack.ranking_pack_id,
                "ranking_pack_fingerprint": (
                    revision.ranking_pack.ranking_pack_sha256
                ),
                "ranking_pack_schema_version": (
                    revision.ranking_pack.schema_version
                ),
                "ranking_pack_verified_at": revision.ranking_pack.verified_at,
                "inventory_revision": revision.inventory_revision,
                "inventory_fingerprint": revision.inventory_fingerprint,
                "created_at": revision.created_at,
            })
        profile_ids = (profile_id,) if profile_id else tuple(sorted(config.profiles))
        events = [
            event.model_dump(mode="json", by_alias=True, warnings=False)
            for item_profile_id in profile_ids
            for event in self.store.list_management_lifecycle_events(
                management_authority_id,
                item_profile_id,
            )
        ]
        receipts: list[dict[str, Any]] = []
        for revision_id in revision_ids:
            rows = self.store.connection.execute(
                "SELECT receipt_id FROM management_config_receipts "
                "WHERE revision_id=? ORDER BY created_at, receipt_id",
                (revision_id,),
            ).fetchall()
            for row in rows:
                receipt = self.store.read_management_receipt(str(row["receipt_id"]))
                if receipt is None:
                    raise AutoRoutingServiceError("management history is unavailable")
                receipts.append(
                    receipt.model_dump(mode="json", by_alias=True, warnings=False)
                )
        return {
            "management_authority_id": management_authority_id,
            "profile_id": profile_id,
            "revision_count": len(revisions),
            "event_count": len(events),
            "receipt_count": len(receipts),
            "revisions": revisions,
            "events": events,
            "receipts": receipts,
        }

    @staticmethod
    def _management_control_proposal(
        config: AutoRoutingConfig,
        *,
        action: str,
        schedule: str | None,
        ranking_pack_path: str | None = None,
        trusted_public_keys: tuple[str, ...] | None = None,
        daily_limit: int | None = None,
    ) -> AutoRoutingConfig:
        settings = config.autonomous_profile_management
        update: dict[str, Any] = {}
        if action == "enable":
            update["enabled"] = True
        elif action == "disable":
            update["enabled"] = False
        elif action == "schedule":
            if not isinstance(schedule, str) or not schedule.strip():
                raise AutoRoutingServiceError("manage schedule requires --schedule")
            parse_schedule(schedule)
            update["schedule"] = schedule.strip()
        elif action == "ranking-trust":
            if not isinstance(ranking_pack_path, str) or not ranking_pack_path.strip():
                raise AutoRoutingServiceError(
                    "manage ranking-trust requires --ranking-pack-path"
                )
            if not trusted_public_keys:
                raise AutoRoutingServiceError(
                    "manage ranking-trust requires at least one trusted key"
                )
            try:
                update["ranking_pack"] = RankingPackTrust(
                    ranking_pack_path=ranking_pack_path.strip(),
                    trusted_ed25519_public_keys=trusted_public_keys,
                )
            except Exception as error:
                raise AutoRoutingServiceError(
                    "requested ranking trust is invalid"
                ) from error
        elif action == "daily-cap":
            update["daily_change_limit"] = daily_limit
        elif action not in {"freeze", "unfreeze", "reconcile"}:
            raise AutoRoutingServiceError(
                f"unsupported management control action: {action}"
            )
        if not update:
            return config
        document = settings.model_dump(mode="python", by_alias=True)
        document.update(update)
        try:
            proposed_settings = AutonomousProfileManagementSettings.model_validate(
                document
            )
        except Exception as error:
            raise AutoRoutingServiceError(
                "requested management settings are invalid"
            ) from error
        return config.model_copy(
            update={"autonomous_profile_management": proposed_settings}
        )

    def _management_reconcile_inputs_precondition(
        self,
        *,
        config: AutoRoutingConfig,
        control: ManagementControl,
    ) -> dict[str, Any]:
        """Fingerprint every local input a manual reconciliation may consume."""
        management_authority_id = management_authority_revision(config)
        try:
            snapshot = self._current_persisted_inventory_snapshot()
            inventory = {
                "reason_code": None,
                "revision": snapshot.revision,
                "fingerprint": self._management_inventory_fingerprint(
                    snapshot.revision
                ),
            }
        except _ManagementHoldError as error:
            inventory = {
                "reason_code": error.reason_code,
                "revision": None,
                "fingerprint": None,
            }
        try:
            latest = self._latest_management_revision(management_authority_id)
            revision = (
                None
                if latest is None
                else {
                    "revision_id": latest.revision_id,
                    "resulting_authority_id": latest.resulting_authority_id,
                    "management_epoch": latest.management_epoch,
                }
            )
            profile_states = [
                {
                    "profile_id": profile_id,
                    "active_revision_id": state.active_revision_id,
                    "control_revision_id": state.control_revision_id,
                    "challenger_revision_id": state.challenger_revision_id,
                    "management_epoch": state.management_epoch,
                    "generation": state.generation,
                    "experiment_phase": state.experiment_phase,
                }
                for profile_id in sorted(config.profiles)
                for state in (
                    self.store.read_management_profile_state(
                        management_authority_id,
                        profile_id,
                        current_authority_id=authority_revision(config),
                    ),
                )
            ]
        except Exception:
            revision = {"reason_code": "management_state_unavailable"}
            profile_states = []
        ranking = self.management_ranking_status()
        return {
            "enabled": config.autonomous_profile_management.enabled,
            "frozen": control.frozen,
            "inventory": inventory,
            "ranking_pack": {
                key: ranking.get(key)
                for key in (
                    "status",
                    "reason_code",
                    "ranking_pack_id",
                    "ranking_pack_sha256",
                    "schema_version",
                )
            },
            "latest_revision": revision,
            "profile_states": profile_states,
        }

    def _management_control_precondition(
        self,
        *,
        config: AutoRoutingConfig,
        action: str,
        schedule: str | None,
        ranking_pack_path: str | None = None,
        trusted_public_keys: tuple[str, ...] | None = None,
        daily_limit: int | None = None,
    ) -> tuple[dict[str, Any], ManagementControl, AutoRoutingConfig]:
        proposal = self._management_control_proposal(
            config,
            action=action,
            schedule=schedule,
            ranking_pack_path=ranking_pack_path,
            trusted_public_keys=trusted_public_keys,
            daily_limit=daily_limit,
        )
        settings = config.autonomous_profile_management
        management_authority_id = management_authority_revision(config)
        control = self.store.read_management_control(management_authority_id)
        recovery_required = any(
            self.store.read_management_profile_state(
                management_authority_id,
                profile_id,
                current_authority_id=authority_revision(config),
            ).experiment_phase
            == "recovery_required"
            for profile_id in sorted(config.profiles)
        )
        pending_finalization = bool(
            self.store.list_pending_management_lifecycle_finalizations()
        )
        if action == "unfreeze" and recovery_required:
            raise AutoRoutingServiceError(
                "management cannot unfreeze while recovery-required profiles remain"
            )
        if pending_finalization and action != "freeze":
            raise AutoRoutingServiceError(
                "management lifecycle finalization requires recovery"
            )
        trust = settings.ranking_pack
        pack_path = None if trust is None else trust.ranking_pack_path
        pack_fingerprint: str | None = None
        if trust is not None:
            status = self.management_ranking_status()
            if status.get("status") == "verified":
                pack_fingerprint = str(status["ranking_pack_sha256"])
        requested_schedule = (
            proposal.autonomous_profile_management.schedule
            if action == "schedule"
            else settings.schedule
        )
        proposed_settings = proposal.autonomous_profile_management
        proposed_trust = proposed_settings.ranking_pack
        proposed_pack_status: dict[str, object] = {}
        trust_summary: dict[str, object] = {
            "trusted_key_count": 0,
            "trusted_key_set_fingerprint": None,
        }
        if proposed_trust is not None:
            if action == "ranking-trust":
                try:
                    trust_summary = ranking_trust_summary(proposed_trust)
                    proposed_pack = load_verified_ranking_pack(
                        home=self.hermes_home,
                        trust=proposed_trust,
                        now=_management_utc_now(None),
                    )
                    proposed_pack_status = proposed_pack.metadata.model_dump(
                        mode="json"
                    )
                except RankingPackError as error:
                    raise AutoRoutingServiceError(error.reason_code) from error
            else:
                configured_keys = tuple(
                    proposed_trust.trusted_ed25519_public_keys
                )
                trust_summary = {
                    "trusted_key_count": len(configured_keys),
                    "trusted_key_set_fingerprint": _checksum(
                        _canonical_json(sorted(configured_keys))
                    ),
                }
                proposed_pack_status = ranking_pack_status(
                    home=self.hermes_home,
                    trust=proposed_trust,
                    now=_management_utc_now(None),
                )
        precondition = {
            "authority_id": authority_revision(config),
            "proposed_authority_id": authority_revision(proposal),
            "config_sha": config_revision(config),
            "proposed_config_sha": config_revision(proposal),
            "management_authority_id": management_authority_id,
            "proposed_management_authority_id": (
                management_authority_revision(proposal)
            ),
            "control_generation": control.generation,
            "action": action,
            "schedule": requested_schedule,
            "ranking_pack_path": pack_path,
            "ranking_pack_fingerprint": pack_fingerprint,
            "daily_change_limit": settings.daily_change_limit,
            "proposed_daily_change_limit": proposed_settings.daily_change_limit,
            "proposed_ranking_pack_id": (proposed_pack_status.get("ranking_pack_id")),
            "proposed_ranking_pack_fingerprint": (
                proposed_pack_status.get("ranking_pack_sha256")
            ),
            "proposed_ranking_pack_schema_version": (
                proposed_pack_status.get("schema_version")
            ),
            **trust_summary,
            "cron_job_id": control.cron_job_id,
        }
        if action == "reconcile":
            precondition["reconcile_inputs"] = (
                self._management_reconcile_inputs_precondition(
                    config=config,
                    control=control,
                )
            )
        return precondition, control, proposal

    def _management_reconcile_precondition_matches(
        self,
        approved_precondition: Mapping[str, Any],
        *,
        config: AutoRoutingConfig | None = None,
    ) -> bool:
        """Require manual reconciliation to retain its exact approved inputs."""
        try:
            current = config if config is not None else self._configured_authority()
            precondition, _control, _proposal = self._management_control_precondition(
                config=current,
                action="reconcile",
                schedule=None,
            )
            return hmac.compare_digest(
                _canonical_json(precondition),
                _canonical_json(dict(approved_precondition)),
            )
        except Exception:
            return False

    def preview_management_control(
        self,
        *,
        action: str,
        schedule: str | None = None,
        ranking_pack_path: str | None = None,
        trusted_public_keys: tuple[str, ...] | None = None,
        daily_limit: int | None = None,
    ) -> dict[str, Any]:
        """Build a complete exact-hash preview for one global mutation."""
        with profile_config_lock(self.config_path):
            config = self._configured_authority()
            precondition, _control, proposal = self._management_control_precondition(
                config=config,
                action=action,
                schedule=schedule,
                ranking_pack_path=ranking_pack_path,
                trusted_public_keys=trusted_public_keys,
                daily_limit=daily_limit,
            )
            return {
                "apply": False,
                "action": action,
                "precondition": precondition,
                "precondition_hash": _checksum(_canonical_json(precondition)),
                "proposed_management_authority_id": (
                    management_authority_revision(proposal)
                ),
            }

    @staticmethod
    def _management_control_event_type(
        current: ManagementControl,
        *,
        frozen: bool,
    ) -> str:
        if current.frozen == frozen:
            return "hold"
        return "frozen" if frozen else "unfrozen"

    def _transition_global_management_control(
        self,
        *,
        config: AutoRoutingConfig,
        current: ManagementControl,
        frozen: bool,
        cron_job_id: str | None,
        action: str,
        moment: datetime,
    ) -> ManagementControl:
        management_authority_id = management_authority_revision(config)
        if current.management_authority_id != management_authority_id:
            current = self.store.read_management_control(management_authority_id)
        created_at = _management_timestamp(moment)
        event_type = self._management_control_event_type(current, frozen=frozen)
        profile_id = sorted(config.profiles)[0]
        event_seed = {
            "kind": "management-global-control-v1",
            "management_authority_id": management_authority_id,
            "profile_id": profile_id,
            "generation": current.generation,
            "action": action,
            "frozen": frozen,
            "cron_job_id": cron_job_id,
            "created_at": created_at,
        }
        return self.store.transition_management_control(
            control=ManagementControl(
                management_authority_id=management_authority_id,
                frozen=frozen,
                changes_today=current.changes_today,
                cron_job_id=cron_job_id,
                generation=current.generation,
                updated_at=created_at,
            ),
            expected_generation=current.generation,
            event=ManagementLifecycleEvent(
                event_id=_checksum(_canonical_json(event_seed)),
                management_authority_id=management_authority_id,
                profile_id=profile_id,
                revision_id=None,
                event_type=event_type,
                reason_code=f"management_{action}",
                created_at=created_at,
            ),
        )

    def _retire_management_authority_canaries(
        self,
        *,
        config: AutoRoutingConfig,
        moment: datetime,
    ) -> None:
        """Settle every canary owned by one superseded management authority."""
        management_authority_id = management_authority_revision(config)
        current_authority_id = authority_revision(config)
        rows = self.store.connection.execute(
            "SELECT profile_id FROM management_profile_states "
            "WHERE management_authority_id=? "
            "AND experiment_phase IN ('validated', 'canary') "
            "ORDER BY profile_id",
            (management_authority_id,),
        ).fetchall()
        for row in rows:
            profile_id = str(row["profile_id"])
            state = self.store.read_management_profile_state(
                management_authority_id,
                profile_id,
            )
            candidates = tuple(
                revision
                for revision_id in (
                    state.challenger_revision_id,
                    state.control_revision_id,
                )
                if revision_id is not None
                for revision in (self.store.read_management_revision(revision_id),)
                if revision is not None
                and revision.resulting_authority_id == current_authority_id
            )
            if len(candidates) != 1:
                raise AutoRoutingServiceError(
                    "management authority canary cannot be rebased exactly"
                )
            target = candidates[0]
            retired_at = _management_timestamp(moment)
            self.store.cancel_stale_management_experiment(
                profile_id=profile_id,
                authority_id=management_authority_id,
                expected_generation=state.generation,
                state=state.model_copy(
                    update={
                        "authority_id": target.resulting_authority_id,
                        "active_revision_id": target.revision_id,
                        "management_epoch": target.management_epoch,
                        "control_revision_id": None,
                        "challenger_revision_id": None,
                        "experiment_phase": "eligible",
                        "cooldown_until": None,
                        "updated_at": retired_at,
                    }
                ),
                event=self._management_event(
                    state=state,
                    revision_id=target.revision_id,
                    event_type="recovered",
                    reason_code="management_authority_changed",
                    created_at=retired_at,
                ),
            )
        open_row = self.store.connection.execute(
            "SELECT assignment_id FROM management_canary_assignments "
            "WHERE management_authority_id=? "
            "AND phase IN ('reserved', 'finalized') LIMIT 1",
            (management_authority_id,),
        ).fetchone()
        if open_row is not None:
            raise AutoRoutingServiceError(
                "management authority retains an open canary assignment"
            )

    def _transition_management_control_authority(
        self,
        *,
        current_config: AutoRoutingConfig,
        proposal: AutoRoutingConfig,
        current: ManagementControl,
        frozen: bool,
        cron_job_id: str | None,
        action: str,
        moment: datetime,
    ) -> ManagementControl:
        """Retire old/returning canaries and publish the new control together."""
        current_management_authority = management_authority_revision(current_config)
        proposed_management_authority = management_authority_revision(proposal)
        with self.store.write_txn():
            if current_management_authority != proposed_management_authority:
                self._retire_management_authority_canaries(
                    config=current_config,
                    moment=moment,
                )
                self._retire_management_authority_canaries(
                    config=proposal,
                    moment=moment,
                )
            return self._transition_global_management_control(
                config=proposal,
                current=current,
                frozen=frozen,
                cron_job_id=cron_job_id,
                action=action,
                moment=moment,
            )

    @staticmethod
    def _require_management_precondition_hash(expected_hash: str) -> None:
        if not isinstance(expected_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_hash
        ):
            raise AutoRoutingServiceError(
                "management apply requires a SHA-256 precondition hash"
            )

    def _management_control_activation_rollover(
        self,
        *,
        current: AutoRoutingConfig,
        proposal: AutoRoutingConfig,
        moment: datetime,
    ) -> ManagementActivationRollover | None:
        """Carry one exact active receipt across a guarded settings mutation."""
        if current.activation.mode != "active":
            if proposal.activation.mode == "active":
                raise AutoRoutingServiceError(
                    "management control cannot activate routing"
                )
            return None
        if proposal.activation.mode != "active":
            raise AutoRoutingServiceError(
                "management control cannot deactivate routing"
            )
        _report, adapter_sha = self._adapter_contract(self.adapter)
        predecessor = self.store.read_matching_activation_receipt(
            authority_id=authority_revision(current),
            config_sha=config_revision(current),
            adapter_capability_sha=adapter_sha,
        )
        if predecessor is None:
            raise AutoRoutingServiceError(
                "management predecessor activation receipt changed"
            )
        self._assert_adapter_capability_unchanged(adapter_sha)
        snapshot = self.store.read_inventory_snapshot(predecessor.inventory_revision)
        if snapshot is None or snapshot.checksum != predecessor.inventory_contract_sha:
            raise AutoRoutingServiceError("management activation inventory changed")
        resulting_authority_id = authority_revision(proposal)
        resulting_config_sha = config_revision(proposal)
        receipt = self.store.read_matching_activation_receipt(
            authority_id=resulting_authority_id,
            config_sha=resulting_config_sha,
            adapter_capability_sha=adapter_sha,
            inventory_contract_sha=predecessor.inventory_contract_sha,
            inventory_revision=predecessor.inventory_revision,
        )
        if receipt is None:
            seed = {
                "kind": "management-activation-contract-v1",
                "authority_id": resulting_authority_id,
                "config_sha": resulting_config_sha,
                "inventory_contract_sha": predecessor.inventory_contract_sha,
                "inventory_revision": predecessor.inventory_revision,
                "adapter_capability_sha": adapter_sha,
            }
            receipt = ActivationReceipt(
                receipt_id=f"activation-{_checksum(_canonical_json(seed))}",
                authority_id=resulting_authority_id,
                config_sha=resulting_config_sha,
                inventory_contract_sha=predecessor.inventory_contract_sha,
                inventory_revision=predecessor.inventory_revision,
                adapter_capability_sha=adapter_sha,
                created_at=_management_timestamp(moment),
            )
        return ManagementActivationRollover(
            predecessor=predecessor,
            receipt=receipt,
            authority_document=authority_document(proposal),
            baseline=self._baseline_revision(
                proposal,
                authority_id=resulting_authority_id,
            ),
        )

    def _publish_management_control_activation_rollover(
        self,
        rollover: ManagementActivationRollover,
    ) -> tuple[bool, bool, bool, bool]:
        """Publish a derived rollover and report which immutable rows were new."""
        self._assert_adapter_capability_unchanged(
            rollover.predecessor.adapter_capability_sha
        )
        persisted = self.store.read_matching_activation_receipt(
            authority_id=rollover.predecessor.authority_id,
            config_sha=rollover.predecessor.config_sha,
            adapter_capability_sha=rollover.predecessor.adapter_capability_sha,
            inventory_contract_sha=rollover.predecessor.inventory_contract_sha,
            inventory_revision=rollover.predecessor.inventory_revision,
        )
        if persisted != rollover.predecessor:
            raise AutoRoutingServiceError(
                "management predecessor activation receipt changed"
            )
        receipt_created = (
            self.store.read_activation_receipt(rollover.receipt.receipt_id) is None
        )
        authority_created = (
            self.store.read_authority_revision(rollover.receipt.authority_id) is None
        )
        baseline_created = (
            self.store.read_revision(rollover.baseline.revision_id) is None
        )
        pointer_created = (
            self.store.read_active_revision(rollover.receipt.authority_id) is None
        )
        created = (
            receipt_created,
            authority_created,
            baseline_created,
            pointer_created,
        )
        with self.store.write_txn():
            self.store.publish_authority_and_baseline(
                authority_id=rollover.receipt.authority_id,
                document=rollover.authority_document,
                baseline=rollover.baseline,
            )
            self.store.write_activation_receipt(rollover.receipt)
        return created

    def _rollback_management_control_activation_rollover(
        self,
        rollover: ManagementActivationRollover,
        created: tuple[bool, bool, bool, bool],
    ) -> None:
        receipt_created, authority_created, baseline_created, pointer_created = created
        if receipt_created:
            self.store.rollback_activation_receipt(rollover.receipt)
        if authority_created or baseline_created or pointer_created:
            self.store.rollback_authority_and_baseline(
                authority_id=rollover.receipt.authority_id,
                baseline_revision_id=rollover.baseline.revision_id,
                remove_authority=authority_created,
                remove_baseline=baseline_created,
                remove_active_pointer=pointer_created,
            )

    def apply_management_control(
        self,
        *,
        action: str,
        expected_hash: str,
        schedule: str | None = None,
        ranking_pack_path: str | None = None,
        trusted_public_keys: tuple[str, ...] | None = None,
        daily_limit: int | None = None,
    ) -> dict[str, Any] | ManagementReconcileReport:
        """Apply one exact global preview under the profile configuration lock."""
        self._require_management_precondition_hash(expected_hash)
        if action == "reconcile":
            with profile_config_lock(self.config_path):
                config = self._configured_authority()
                precondition, _control, _proposal = (
                    self._management_control_precondition(
                        config=config,
                        action=action,
                        schedule=schedule,
                        ranking_pack_path=ranking_pack_path,
                        trusted_public_keys=trusted_public_keys,
                        daily_limit=daily_limit,
                    )
                )
                actual_hash = _checksum(_canonical_json(precondition))
                if not hmac.compare_digest(actual_hash, expected_hash):
                    raise AutoRoutingServiceError(
                        "management precondition changed; run the preview again"
                    )
            return self.reconcile_management(
                scheduled=False,
                approved_precondition=precondition,
            )

        current_config = self._configured_authority()
        proposal = self._management_control_proposal(
            current_config,
            action=action,
            schedule=schedule,
            ranking_pack_path=ranking_pack_path,
            trusted_public_keys=trusted_public_keys,
            daily_limit=daily_limit,
        )
        moment = _management_utc_now(None)
        if action in {"freeze", "unfreeze"}:
            with profile_config_lock(self.config_path):
                locked_config = self._configured_authority()
                precondition, control, _proposal = (
                    self._management_control_precondition(
                        config=locked_config,
                        action=action,
                        schedule=schedule,
                        ranking_pack_path=ranking_pack_path,
                        trusted_public_keys=trusted_public_keys,
                        daily_limit=daily_limit,
                    )
                )
                actual_hash = _checksum(_canonical_json(precondition))
                if not hmac.compare_digest(actual_hash, expected_hash):
                    raise AutoRoutingServiceError(
                        "management precondition changed; run the preview again"
                    )
                updated = self._transition_global_management_control(
                    config=locked_config,
                    current=control,
                    frozen=action == "freeze",
                    cron_job_id=control.cron_job_id,
                    action=action,
                    moment=moment,
                )
            return {
                **self.management_status(now=moment),
                "apply": True,
                "action": action,
                "applied_precondition_hash": expected_hash,
                "control_generation": updated.generation,
            }

        backup_path = self.config_path.with_name(
            f"{self.config_path.name}.auto-routing.management-control."
            f"{uuid.uuid4().hex}.bak"
        )
        with locked_update(
            proposal,
            path=self.config_path,
            allow_active=True,
        ) as mutation:
            locked_config = mutation.current_config()
            locked_proposal = self._management_control_proposal(
                locked_config,
                action=action,
                schedule=schedule,
                ranking_pack_path=ranking_pack_path,
                trusted_public_keys=trusted_public_keys,
                daily_limit=daily_limit,
            )
            if config_document(locked_proposal) != config_document(proposal):
                raise AutoRoutingServiceError(
                    "management authority changed; run the preview again"
                )
            precondition, control, _proposal = self._management_control_precondition(
                config=locked_config,
                action=action,
                schedule=schedule,
                ranking_pack_path=ranking_pack_path,
                trusted_public_keys=trusted_public_keys,
                daily_limit=daily_limit,
            )
            actual_hash = _checksum(_canonical_json(precondition))
            if not hmac.compare_digest(actual_hash, expected_hash):
                raise AutoRoutingServiceError(
                    "management precondition changed; run the preview again"
                )
            replaced = mutation.preview.before_bytes != mutation.preview.after_bytes
            installed_cron_mutation: ManagementCronInstall | None = None
            activation_rollover = (
                self._management_control_activation_rollover(
                    current=locked_config,
                    proposal=locked_proposal,
                    moment=moment,
                )
                if replaced
                else None
            )
            rollover_created = (False, False, False, False)

            def restore_cron_after_failure() -> None:
                if action not in {"enable", "schedule"} or installed_cron_mutation is None:
                    return
                if not rollback_management_cron_install(
                    home=self.hermes_home,
                    installed=installed_cron_mutation,
                ):
                    raise RuntimeError("management cron rollback is incomplete")

            def restore_config_after_failure() -> None:
                if replaced:
                    mutation.restore(backup_path)
                backup_path.unlink(missing_ok=True)

            def recover_after_failure() -> None:
                recovery_errors: list[BaseException] = []
                try:
                    restore_cron_after_failure()
                except BaseException as recovery_error:
                    recovery_errors.append(recovery_error)
                try:
                    restore_config_after_failure()
                except BaseException as recovery_error:
                    recovery_errors.append(recovery_error)
                else:
                    if activation_rollover is not None:
                        try:
                            self._rollback_management_control_activation_rollover(
                                activation_rollover,
                                rollover_created,
                            )
                        except BaseException as recovery_error:
                            recovery_errors.append(recovery_error)
                if recovery_errors:
                    raise recovery_errors[0]

            try:
                mutation.create_backup(backup_path)
                if activation_rollover is not None:
                    rollover_created = (
                        self._publish_management_control_activation_rollover(
                            activation_rollover
                        )
                    )
                cron_job_id = control.cron_job_id
                if (
                    action in {"enable", "schedule"}
                    and locked_proposal.autonomous_profile_management.enabled
                ):
                    def remember_installed_job(
                        installed: ManagementCronInstall,
                    ) -> None:
                        nonlocal installed_cron_mutation
                        installed_cron_mutation = installed

                    installed = install_management_cron(
                        home=self.hermes_home,
                        schedule=(
                            locked_proposal.autonomous_profile_management.schedule
                        ),
                        previous_job_id=control.cron_job_id,
                        on_installed=remember_installed_job,
                    )
                    cron_job_id = installed.job_id
                updated = self._transition_management_control_authority(
                    current_config=locked_config,
                    proposal=locked_proposal,
                    current=control,
                    frozen=control.frozen,
                    cron_job_id=None if action == "disable" else cron_job_id,
                    action=action,
                    moment=moment,
                )
                if replaced:
                    mutation.replace()
                if action == "disable":
                    try:
                        removed = remove_management_cron(
                            home=self.hermes_home,
                            job_id=control.cron_job_id,
                        )
                    except BaseException:
                        removed = False
                    if not removed:
                        try:
                            self._transition_global_management_control(
                                config=locked_proposal,
                                current=updated,
                                frozen=True,
                                cron_job_id=control.cron_job_id,
                                action="disable_repair",
                                moment=moment + timedelta(microseconds=1),
                            )
                        except BaseException as recovery_error:
                            raise AutoRoutingServiceError(
                                "management_cron_removal_failed: management is "
                                "disabled but repair-state persistence is incomplete"
                            ) from recovery_error
                        raise AutoRoutingServiceError(
                            "management_cron_removal_failed: management is disabled "
                            "and frozen; repair the recorded cron job before retrying"
                        )
            except AutoRoutingServiceError as error:
                if action == "disable" and "management_cron_removal_failed" in str(error):
                    raise
                try:
                    recover_after_failure()
                except BaseException as recovery_error:
                    raise AutoRoutingServiceError(
                        "management control apply failed and recovery is incomplete"
                    ) from recovery_error
                raise
            except BaseException as error:
                try:
                    recover_after_failure()
                except BaseException as recovery_error:
                    raise AutoRoutingServiceError(
                        "management control apply failed and recovery is incomplete"
                    ) from recovery_error
                if isinstance(error, Exception):
                    raise AutoRoutingServiceError(
                        "management control apply failed"
                    ) from error
                raise
        return {
            **self.management_status(now=moment),
            "apply": True,
            "action": action,
            "applied_precondition_hash": expected_hash,
            "control_generation": updated.generation,
            "backup_path": str(backup_path),
        }

    def _latest_management_revision(
        self,
        management_authority_id: str,
    ) -> ManagementRevision | None:
        row = self.store.connection.execute(
            "SELECT revision_id FROM management_revisions "
            "WHERE management_authority_id=? "
            "ORDER BY management_epoch DESC, created_at DESC, revision_id DESC LIMIT 1",
            (management_authority_id,),
        ).fetchone()
        if row is None:
            return None
        revision = self.store.read_management_revision(str(row["revision_id"]))
        if revision is None:
            raise _ManagementHoldError("management_state_unavailable")
        return revision

    @staticmethod
    def _management_profile_patches(
        *,
        before: AutoRoutingConfig,
        after: AutoRoutingConfig,
        changed_profile_id: str,
        reason_code: str,
    ) -> tuple[ManagementPatch, ...]:
        """Bind every profile to the global tip while identifying one mutation."""
        profile_ids = (
            changed_profile_id,
            *(
                profile_id
                for profile_id in sorted(before.profiles)
                if profile_id != changed_profile_id
            ),
        )
        if set(profile_ids) != set(after.profiles):
            raise _ManagementHoldError("profile_topology_changed")
        patches: list[ManagementPatch] = []
        for profile_id in profile_ids:
            before_ids = AutoRoutingService._management_runtime_ids(
                before.profiles[profile_id]
            )
            after_ids = AutoRoutingService._management_runtime_ids(
                after.profiles[profile_id]
            )
            patches.append(
                ManagementPatch(
                    profile_id=profile_id,
                    before_runtime_ids=before_ids,
                    after_runtime_ids=after_ids,
                    reason_codes=(
                        reason_code
                        if profile_id == changed_profile_id
                        else "authority_control_snapshot",
                    ),
                )
            )
        return tuple(patches)

    def _management_control_revision(
        self,
        *,
        config: AutoRoutingConfig,
        profile_id: str,
        snapshot: InventorySnapshot,
        pack: VerifiedRankingPack,
        parent: ManagementRevision | None,
        now: datetime,
    ) -> ManagementRevision | None:
        """Return an exact current-tip control, creating a baseline when needed."""
        current_authority_id = authority_revision(config)
        if parent is not None and parent.resulting_authority_id == current_authority_id:
            if self._management_revision_patch(parent, profile_id) is None:
                raise _ManagementHoldError("management_baseline_unavailable")
            return None
        if parent is None:
            preceding_authority_id = "0" * 64
            before = config
        else:
            preceding_authority_id = parent.resulting_authority_id
            before_profiles: dict[str, RouteProfile] = {}
            for candidate_profile_id, current_profile in config.profiles.items():
                previous_patch = self._management_revision_patch(
                    parent, candidate_profile_id
                )
                if previous_patch is None:
                    raise _ManagementHoldError("management_rebase_unprovable")
                current_ids = self._management_runtime_ids(current_profile)
                if previous_patch.after_runtime_ids == current_ids:
                    before_profiles[candidate_profile_id] = current_profile
                    continue
                # Runtime membership changed outside management.  Its prior
                # typed targets are not recoverable from identifiers alone.
                raise _ManagementHoldError("management_rebase_unprovable")
            before = config.model_copy(update={"profiles": before_profiles})
        patches = self._management_profile_patches(
            before=before,
            after=config,
            changed_profile_id=profile_id,
            reason_code=(
                "management_control_baseline"
                if parent is None
                else "manual_authority_rebase"
            ),
        )
        inventory_fingerprint = self._management_inventory_fingerprint(
            snapshot.revision
        )
        created_at = _management_timestamp(now)
        epoch = 1 if parent is None else parent.management_epoch + 1
        seed = {
            "kind": "management-control-baseline-v1",
            "preceding_authority_id": preceding_authority_id,
            "resulting_authority_id": current_authority_id,
            "management_authority_id": management_authority_revision(config),
            "parent_revision_id": None if parent is None else parent.revision_id,
            "profile_id": profile_id,
            "patches": [patch.model_dump(mode="json") for patch in patches],
            "ranking_pack": pack.metadata.model_dump(mode="json"),
            "inventory_revision": snapshot.revision,
            "inventory_fingerprint": inventory_fingerprint,
            "management_epoch": epoch,
            "created_at": created_at,
        }
        return ManagementRevision(
            revision_id=_checksum(_canonical_json(seed)),
            preceding_authority_id=preceding_authority_id,
            resulting_authority_id=current_authority_id,
            management_authority_id=management_authority_revision(config),
            parent_revision_id=None if parent is None else parent.revision_id,
            ranking_pack=pack.metadata,
            inventory_revision=snapshot.revision,
            inventory_fingerprint=inventory_fingerprint,
            management_epoch=epoch,
            action="fallback_reorder",
            patches=patches,
            created_at=created_at,
        )

    @staticmethod
    def _management_profile_result(
        profile_id: str,
        result: ManagementRevisionResult,
    ) -> ManagementProfileReconcileResult:
        return ManagementProfileReconcileResult(
            profile_id=profile_id,
            changed=result.changed,
            reason_code=result.reason_code,
            revision_id=result.revision_id,
        )

    def _prepare_management_profile(
        self,
        *,
        config: AutoRoutingConfig,
        profile_id: str,
        snapshot: InventorySnapshot,
        pack: VerifiedRankingPack,
        now: datetime,
    ) -> _PreparedManagementPlan | ManagementProfileReconcileResult:
        profile = config.profiles.get(profile_id)
        if profile is None:
            return ManagementProfileReconcileResult(
                profile_id, False, "profile_unavailable"
            )
        management_authority_id = management_authority_revision(config)
        preceding_authority_id = authority_revision(config)
        control = self.store.read_management_control(management_authority_id)
        if control.frozen:
            return ManagementProfileReconcileResult(
                profile_id, False, "management_frozen"
            )
        state = self.store.read_management_profile_state(
            management_authority_id,
            profile_id,
            current_authority_id=preceding_authority_id,
        )
        if state.experiment_phase != "eligible":
            return ManagementProfileReconcileResult(
                profile_id, False, "management_state_not_eligible"
            )
        if state.active_revision_id is not None:
            active = self.store.read_management_revision(state.active_revision_id)
            if (
                active is not None
                and active.action == "propose_canary"
                and active.resulting_authority_id == preceding_authority_id
            ):
                return ManagementProfileReconcileResult(
                    profile_id, False, "management_canary_pending"
                )
        assignments = self.store.list_open_management_assignments(
            management_authority_id,
            profile_id,
        )
        candidates = verified_inventory_candidates(snapshot, now)
        plan = plan_management_revision(
            profile=profile,
            candidates=candidates,
            pack=pack,
            active_assignments=assignments,
            now=now,
        )
        if plan.action in {"hold", "no_change"} or plan.after_profile is None:
            reason = plan.reason_codes[0] if plan.reason_codes else plan.action
            return ManagementProfileReconcileResult(profile_id, False, reason)
        if plan.patch is None:
            return ManagementProfileReconcileResult(
                profile_id, False, "unsafe_mutation"
            )
        proposal = config.model_copy(
            update={
                "profiles": {
                    **config.profiles,
                    profile_id: plan.after_profile,
                }
            }
        )
        resulting_authority_id = authority_revision(proposal)
        parent = self._latest_management_revision(management_authority_id)
        try:
            control_revision = self._management_control_revision(
                config=config,
                profile_id=profile_id,
                snapshot=snapshot,
                pack=pack,
                parent=parent,
                now=now,
            )
        except _ManagementHoldError as error:
            return ManagementProfileReconcileResult(
                profile_id, False, error.reason_code
            )
        control_tip = control_revision or parent
        if control_tip is None:
            return ManagementProfileReconcileResult(
                profile_id, False, "management_baseline_unavailable"
            )
        ranked = rank_management_candidates(profile, candidates, pack)
        scores = tuple(
            (candidate.runtime_id, candidate.score)
            for candidate in ranked
            if candidate.eligible and candidate.score is not None
        )
        inventory_fingerprint = self._management_inventory_fingerprint(
            snapshot.revision
        )
        created_at = _management_timestamp(now)
        management_epoch = control_tip.management_epoch + 1
        revision_patches = self._management_profile_patches(
            before=config,
            after=proposal,
            changed_profile_id=profile_id,
            reason_code=plan.patch.reason_codes[0],
        )
        revision_seed = {
            "kind": "management-revision-v1",
            "preceding_authority_id": preceding_authority_id,
            "resulting_authority_id": resulting_authority_id,
            "management_authority_id": management_authority_id,
            "parent_revision_id": control_tip.revision_id,
            "ranking_pack": pack.metadata.model_dump(mode="json"),
            "inventory_revision": snapshot.revision,
            "inventory_fingerprint": inventory_fingerprint,
            "management_epoch": management_epoch,
            "action": plan.action,
            "patches": [patch.model_dump(mode="json") for patch in revision_patches],
            "runtime_scores": scores,
            "created_at": created_at,
        }
        revision = ManagementRevision(
            revision_id=_checksum(_canonical_json(revision_seed)),
            preceding_authority_id=preceding_authority_id,
            resulting_authority_id=resulting_authority_id,
            management_authority_id=management_authority_id,
            parent_revision_id=control_tip.revision_id,
            ranking_pack=pack.metadata,
            inventory_revision=snapshot.revision,
            inventory_fingerprint=inventory_fingerprint,
            management_epoch=management_epoch,
            action=plan.action,
            patches=revision_patches,
            runtime_scores=scores,
            created_at=created_at,
        )
        return _PreparedManagementPlan(
            profile_id=profile_id,
            proposal=proposal,
            revision=revision,
            control_revision=control_revision,
            expected_authority_id=preceding_authority_id,
            expected_control_generation=control.generation,
            planned_at=now,
        )

    def _apply_prepared_management_plan(
        self,
        prepared: _PreparedManagementPlan,
        *,
        now: datetime,
        approved_precondition: Mapping[str, Any] | None = None,
        scheduled_invocation: _ScheduledManagementInvocation | None = None,
    ) -> ManagementProfileReconcileResult:
        precommit_check = None
        if approved_precondition is not None:
            def precommit_check(current: AutoRoutingConfig) -> str | None:
                if (
                    approved_precondition is not None
                    and not self._management_reconcile_precondition_matches(
                        approved_precondition,
                        config=current,
                    )
                ):
                    return "management_precondition_changed"
                return None

        mutation_guard = None
        if scheduled_invocation is not None:
            def mutation_guard(current: AutoRoutingConfig):
                return self._scheduled_management_mutation_guard(
                    scheduled_invocation,
                    config=current,
                )

        try:
            current = self._configured_authority()
            activation_rollover = self._management_activation_rollover(
                current=current,
                proposal=prepared.proposal,
                revision=prepared.revision,
            )
        except Exception:
            return ManagementProfileReconcileResult(
                prepared.profile_id,
                False,
                "activation_receipt_changed",
            )
        result = apply_management_config_revision(
            proposal=prepared.proposal,
            revision=prepared.revision,
            expected_authority_id=prepared.expected_authority_id,
            admission_utc_day=_management_utc_now(now).date().isoformat(),
            store=self.store,
            config_path=self.config_path,
            expected_control_generation=prepared.expected_control_generation,
            control_revision=prepared.control_revision,
            activation_rollover=activation_rollover,
            precommit_check=precommit_check,
            mutation_guard=mutation_guard,
        )
        return self._management_profile_result(prepared.profile_id, result)

    def _management_activation_rollover(
        self,
        *,
        current: AutoRoutingConfig,
        proposal: AutoRoutingConfig,
        revision: ManagementRevision,
    ) -> ManagementActivationRollover | None:
        """Derive one active receipt solely from exact persisted local evidence."""
        if current.activation.mode != "active":
            if proposal.activation.mode == "active":
                raise AutoRoutingServiceError("management cannot activate routing")
            return None
        if proposal.activation.mode != "active":
            raise AutoRoutingServiceError("management cannot deactivate routing")
        if authority_revision(current) != revision.preceding_authority_id:
            raise AutoRoutingServiceError("management predecessor authority changed")
        _report, adapter_sha = self._adapter_contract(self.adapter)
        predecessor = self.store.read_matching_activation_receipt(
            authority_id=revision.preceding_authority_id,
            config_sha=config_revision(current),
            adapter_capability_sha=adapter_sha,
        )
        if predecessor is None:
            raise AutoRoutingServiceError(
                "management predecessor activation receipt changed"
            )
        self._assert_adapter_capability_unchanged(predecessor.adapter_capability_sha)
        snapshot = self.store.read_inventory_snapshot(revision.inventory_revision)
        if snapshot is None or snapshot.checksum != revision.inventory_fingerprint:
            raise AutoRoutingServiceError("management activation inventory changed")
        resulting_authority_id = authority_revision(proposal)
        resulting_config_sha = config_revision(proposal)
        receipt = self.store.read_matching_activation_receipt(
            authority_id=resulting_authority_id,
            config_sha=resulting_config_sha,
            adapter_capability_sha=adapter_sha,
            inventory_contract_sha=revision.inventory_fingerprint,
            inventory_revision=revision.inventory_revision,
        )
        if receipt is None:
            seed = {
                "kind": "management-activation-contract-v1",
                "authority_id": resulting_authority_id,
                "config_sha": resulting_config_sha,
                "inventory_contract_sha": revision.inventory_fingerprint,
                "inventory_revision": revision.inventory_revision,
                "adapter_capability_sha": adapter_sha,
            }
            receipt = ActivationReceipt(
                receipt_id=f"activation-{_checksum(_canonical_json(seed))}",
                authority_id=resulting_authority_id,
                config_sha=resulting_config_sha,
                inventory_contract_sha=revision.inventory_fingerprint,
                inventory_revision=revision.inventory_revision,
                adapter_capability_sha=adapter_sha,
                created_at=revision.created_at,
            )
        return ManagementActivationRollover(
            predecessor=predecessor,
            receipt=receipt,
            authority_document=authority_document(proposal),
            baseline=self._baseline_revision(
                proposal,
                authority_id=resulting_authority_id,
            ),
        )

    def _refresh_prepared_management_plan(
        self,
        prepared: _PreparedManagementPlan,
        *,
        now: datetime,
    ) -> _PreparedManagementPlan | ManagementProfileReconcileResult:
        """Re-materialize one cached plan from current local evidence only."""
        stale = ManagementProfileReconcileResult(
            prepared.profile_id,
            False,
            "management_plan_stale",
        )
        try:
            inputs = self._management_inputs(now=now, scheduled=False)
        except Exception:
            return stale
        if isinstance(inputs, ManagementReconcileReport):
            return ManagementProfileReconcileResult(
                prepared.profile_id,
                False,
                inputs.reason_code,
            )
        config, snapshot, pack = inputs
        if authority_revision(config) != prepared.expected_authority_id:
            return ManagementProfileReconcileResult(
                prepared.profile_id,
                False,
                "authority_changed",
            )
        try:
            inventory_fingerprint = self._management_inventory_fingerprint(
                snapshot.revision
            )
        except Exception:
            return stale
        if (
            snapshot.revision != prepared.revision.inventory_revision
            or inventory_fingerprint != prepared.revision.inventory_fingerprint
            or pack.metadata.ranking_pack_id
            != prepared.revision.ranking_pack.ranking_pack_id
            or pack.metadata.ranking_pack_sha256
            != prepared.revision.ranking_pack.ranking_pack_sha256
            or pack.metadata.schema_version
            != prepared.revision.ranking_pack.schema_version
        ):
            return stale
        try:
            refreshed = self._prepare_management_profile(
                config=config,
                profile_id=prepared.profile_id,
                snapshot=snapshot,
                pack=pack,
                now=now,
            )
        except Exception:
            return stale
        if isinstance(refreshed, ManagementProfileReconcileResult):
            if refreshed.reason_code in {
                "authority_changed",
                "management_frozen",
                "management_state_unavailable",
            }:
                return refreshed
            return stale
        if (
            refreshed.expected_control_generation
            != prepared.expected_control_generation
        ):
            return ManagementProfileReconcileResult(
                prepared.profile_id,
                False,
                "management_control_changed",
            )
        prepared_control = prepared.control_revision
        refreshed_control = refreshed.control_revision
        if (prepared_control is None) != (refreshed_control is None):
            return stale
        if prepared_control is not None and refreshed_control is not None:
            prepared_control_data = prepared_control.model_dump(mode="json")
            refreshed_control_data = refreshed_control.model_dump(mode="json")
            for data in (prepared_control_data, refreshed_control_data):
                data.pop("revision_id")
                data.pop("created_at")
            if prepared_control_data != refreshed_control_data:
                return stale
        prepared_revision_data = prepared.revision.model_dump(mode="json")
        refreshed_revision_data = refreshed.revision.model_dump(mode="json")
        for data in (prepared_revision_data, refreshed_revision_data):
            data.pop("revision_id")
            data.pop("created_at")
            if prepared_control is not None:
                # The parent is the just-materialized control revision, whose
                # identity includes the apply timestamp by design.
                data.pop("parent_revision_id")
        if (
            refreshed.proposal != prepared.proposal
            or refreshed.expected_authority_id != prepared.expected_authority_id
            or refreshed_revision_data != prepared_revision_data
        ):
            return stale
        return refreshed

    @staticmethod
    def _management_report(
        results: tuple[ManagementProfileReconcileResult, ...],
        *,
        now: datetime,
        scheduled: bool,
    ) -> ManagementReconcileReport:
        changed = tuple(result for result in results if result.changed)
        if changed:
            reason_code = (
                changed[0].reason_code
                if len(changed) == len(results) == 1
                else "partial_reconciliation"
            )
            revision_id = changed[0].revision_id if len(changed) == 1 else None
        elif results and len({result.reason_code for result in results}) == 1:
            reason_code = results[0].reason_code
            revision_id = None
        else:
            reason_code = "reconciliation_held"
            revision_id = None
        return ManagementReconcileReport(
            changed=bool(changed),
            reason_code=reason_code,
            revision_id=revision_id,
            profiles=results,
            scheduled=scheduled,
            reconciled_at=_management_timestamp(now),
        )

    def _management_inputs(
        self,
        *,
        now: datetime,
        scheduled: bool,
    ) -> tuple[AutoRoutingConfig, InventorySnapshot, VerifiedRankingPack] | ManagementReconcileReport:
        try:
            config = self._configured_authority()
        except AutoRoutingServiceError:
            return ManagementReconcileReport.hold(
                "management_config_invalid", now=now, scheduled=scheduled
            )
        settings = config.autonomous_profile_management
        if not settings.enabled:
            return ManagementReconcileReport.hold(
                "management_disabled", now=now, scheduled=scheduled
            )
        management_authority_id = management_authority_revision(config)
        try:
            if self.store.list_pending_management_lifecycle_finalizations():
                return ManagementReconcileReport.hold(
                    "management_recovery_required",
                    now=now,
                    scheduled=scheduled,
                )
            control = self.store.read_management_control(management_authority_id)
        except Exception:
            return ManagementReconcileReport.hold(
                "management_state_unavailable", now=now, scheduled=scheduled
            )
        if control.frozen:
            return ManagementReconcileReport.hold(
                "management_frozen", now=now, scheduled=scheduled
            )
        try:
            snapshot = self._current_persisted_inventory_snapshot()
        except _ManagementHoldError as error:
            return ManagementReconcileReport.hold(
                error.reason_code, now=now, scheduled=scheduled
            )
        trust = settings.ranking_pack
        if trust is None:
            return ManagementReconcileReport.hold(
                "ranking_pack_trust_missing", now=now, scheduled=scheduled
            )
        try:
            pack = load_verified_ranking_pack(
                home=self.hermes_home,
                trust=trust,
                now=now,
            )
        except RankingPackError as error:
            return ManagementReconcileReport.hold(
                error.reason_code, now=now, scheduled=scheduled
            )
        except Exception:
            return ManagementReconcileReport.hold(
                "ranking_pack_unavailable", now=now, scheduled=scheduled
            )
        return config, snapshot, pack

    def reconcile_management(
        self,
        *,
        now: datetime | None = None,
        scheduled: bool = False,
        approved_precondition: Mapping[str, Any] | None = None,
        scheduled_invocation: _ScheduledManagementInvocation | None = None,
    ) -> ManagementReconcileReport:
        """Run one local, refresh-free management pass over independent profiles."""
        moment = _management_utc_now(now)
        if scheduled != (scheduled_invocation is not None):
            return ManagementReconcileReport.hold(
                "scheduled_invocation_required",
                now=moment,
                scheduled=scheduled,
            )
        if self.store.list_pending_management_lifecycle_finalizations():
            return ManagementReconcileReport.hold(
                "management_recovery_required",
                now=moment,
                scheduled=scheduled,
            )
        if (
            scheduled_invocation is not None
            and not self._scheduled_management_invocation_matches(
                scheduled_invocation
            )
        ):
            return ManagementReconcileReport.hold(
                "scheduled_invocation_changed",
                now=moment,
                scheduled=scheduled,
            )
        if (
            approved_precondition is not None
            and not self._management_reconcile_precondition_matches(
                approved_precondition
            )
        ):
            return ManagementReconcileReport.hold(
                "management_precondition_changed",
                now=moment,
                scheduled=scheduled,
            )
        inputs = self._management_inputs(now=moment, scheduled=scheduled)
        if isinstance(inputs, ManagementReconcileReport):
            return inputs
        initial_config, snapshot, pack = inputs
        if (
            approved_precondition is not None
            and not self._management_reconcile_precondition_matches(
                approved_precondition,
                config=initial_config,
            )
        ):
            return ManagementReconcileReport.hold(
                "management_precondition_changed",
                now=moment,
                scheduled=scheduled,
            )
        management_authority_id = management_authority_revision(initial_config)
        current_authority_id = authority_revision(initial_config)
        for profile_id in sorted(initial_config.profiles):
            state = self.store.read_management_profile_state(
                management_authority_id,
                profile_id,
                current_authority_id=current_authority_id,
            )
            if state.experiment_phase not in {"validated", "canary"}:
                continue
            challenger = self.store.read_management_revision(
                state.challenger_revision_id or ""
            )
            if (
                challenger is not None
                and challenger.resulting_authority_id == current_authority_id
            ):
                continue
            recovered_at = _management_timestamp(moment)
            try:
                parent = self._latest_management_revision(
                    management_authority_id
                )
                try:
                    rebased = self._management_control_revision(
                        config=initial_config,
                        profile_id=profile_id,
                        snapshot=snapshot,
                        pack=pack,
                        parent=parent,
                        now=moment,
                    )
                except _ManagementHoldError:
                    rebased = None
                target = rebased or (
                    parent
                    if parent is not None
                    and parent.resulting_authority_id == current_authority_id
                    and self._management_revision_patch(parent, profile_id)
                    is not None
                    else self.store.read_management_revision(
                        state.control_revision_id or ""
                    )
                )
                if target is None:
                    raise AutoRoutingServiceError(
                        "stale management control revision is unavailable"
                    )
                with self.store.write_txn():
                    if rebased is not None:
                        self.store.publish_management_revision(rebased)
                    self.store.cancel_stale_management_experiment(
                        profile_id=profile_id,
                        authority_id=management_authority_id,
                        expected_generation=state.generation,
                        state=state.model_copy(
                            update={
                                "authority_id": target.resulting_authority_id,
                                "active_revision_id": target.revision_id,
                                "management_epoch": target.management_epoch,
                                "control_revision_id": None,
                                "challenger_revision_id": None,
                                "experiment_phase": "eligible",
                                "cooldown_until": None,
                                "updated_at": recovered_at,
                            }
                        ),
                        event=self._management_event(
                            state=state,
                            revision_id=target.revision_id,
                            event_type="recovered",
                            reason_code="manual_authority_changed",
                            created_at=recovered_at,
                        ),
                    )
            except Exception:
                return self._management_report(
                    tuple(
                        ManagementProfileReconcileResult(
                            candidate_profile_id,
                            False,
                            "management_stale_canary_recovery_failed",
                        )
                        for candidate_profile_id in sorted(initial_config.profiles)
                    ),
                    now=moment,
                    scheduled=scheduled,
                )
        canary_states = self.store.connection.execute(
            "SELECT experiment_phase, active_revision_id, challenger_revision_id "
            "FROM management_profile_states "
            "WHERE management_authority_id=? AND ("
            "experiment_phase IN ('validated', 'canary') OR "
            "(experiment_phase='eligible' AND active_revision_id IS NOT NULL))",
            (management_authority_id,),
        ).fetchall()
        canary_active = any(
            str(row["experiment_phase"]) in {"validated", "canary"}
            and revision is not None
            and revision.resulting_authority_id == current_authority_id
            for row, revision in (
                (
                    row,
                    self.store.read_management_revision(
                        str(row["challenger_revision_id"])
                    ),
                )
                for row in canary_states
                if row["challenger_revision_id"] is not None
            )
        )
        canary_pending = any(
            str(row["experiment_phase"]) == "eligible"
            and revision is not None
            and revision.action == "propose_canary"
            and revision.resulting_authority_id == current_authority_id
            for row, revision in (
                (
                    row,
                    self.store.read_management_revision(
                        str(row["active_revision_id"])
                    ),
                )
                for row in canary_states
                if row["active_revision_id"] is not None
            )
        )
        if canary_active or canary_pending:
            reason_code = (
                "management_canary_active"
                if canary_active
                else "management_canary_pending"
            )
            return self._management_report(
                tuple(
                    ManagementProfileReconcileResult(
                        profile_id, False, reason_code
                    )
                    for profile_id in sorted(initial_config.profiles)
                ),
                now=moment,
                scheduled=scheduled,
            )
        owner_seed = {
            "kind": "management-reconcile-owner-v1",
            "management_authority_id": management_authority_id,
            "at": _management_timestamp(moment),
            "nonce": uuid.uuid4().hex,
        }
        owner_id = _checksum(_canonical_json(owner_seed))
        results: list[ManagementProfileReconcileResult] = []
        canary_proposed = False
        for profile_id in sorted(initial_config.profiles):
            if canary_proposed:
                results.append(
                    ManagementProfileReconcileResult(
                        profile_id, False, "management_canary_pending"
                    )
                )
                continue
            if (
                approved_precondition is not None
                and not self._management_reconcile_precondition_matches(
                    approved_precondition
                )
            ):
                results.append(
                    ManagementProfileReconcileResult(
                        profile_id,
                        False,
                        "management_precondition_changed",
                    )
                )
                continue
            try:
                control = self.store.read_management_control(
                    management_authority_id
                )
            except Exception:
                results.append(
                    ManagementProfileReconcileResult(
                        profile_id, False, "management_state_unavailable"
                    )
                )
                continue
            if control.frozen:
                results.append(
                    ManagementProfileReconcileResult(
                        profile_id, False, "management_frozen"
                    )
                )
                continue
            try:
                lease = self.store.acquire_management_lease(
                    management_authority_id,
                    profile_id,
                    owner_id,
                    moment,
                    60.0,
                )
            except Exception:
                lease = None
            if lease is None:
                try:
                    denied_control = self.store.read_management_control(
                        management_authority_id
                    )
                    denied_reason = (
                        "management_frozen"
                        if denied_control.frozen
                        else "management_lease_unavailable"
                    )
                except Exception:
                    denied_reason = "management_lease_unavailable"
                results.append(
                    ManagementProfileReconcileResult(
                        profile_id, False, denied_reason
                    )
                )
                continue
            profile_result: ManagementProfileReconcileResult
            applied_action: str | None = None
            try:
                try:
                    current = self._configured_authority()
                    if (
                        management_authority_revision(current)
                        != management_authority_id
                    ):
                        prepared: _PreparedManagementPlan | ManagementProfileReconcileResult = (
                            ManagementProfileReconcileResult(
                                profile_id, False, "authority_changed"
                            )
                        )
                    elif (
                        scheduled_invocation is not None
                        and not self._scheduled_management_invocation_matches(
                            scheduled_invocation,
                            config=current,
                        )
                    ):
                        prepared = ManagementProfileReconcileResult(
                            profile_id,
                            False,
                            "scheduled_invocation_changed",
                        )
                    else:
                        prepared = self._prepare_management_profile(
                            config=current,
                            profile_id=profile_id,
                            snapshot=snapshot,
                            pack=pack,
                            now=moment,
                        )
                except Exception:
                    prepared = ManagementProfileReconcileResult(
                        profile_id, False, "management_state_unavailable"
                    )
                if isinstance(prepared, ManagementProfileReconcileResult):
                    profile_result = prepared
                else:
                    if (
                        approved_precondition is not None
                        and not self._management_reconcile_precondition_matches(
                            approved_precondition
                        )
                    ):
                        profile_result = ManagementProfileReconcileResult(
                            profile_id,
                            False,
                            "management_precondition_changed",
                        )
                    else:
                        profile_result = self._apply_prepared_management_plan(
                            prepared,
                            now=moment,
                            approved_precondition=approved_precondition,
                            scheduled_invocation=scheduled_invocation,
                        )
                        applied_action = prepared.revision.action
            finally:
                try:
                    released = self.store.release_management_lease(lease)
                except Exception:
                    released = False
            if not released:
                profile_result = replace(
                    profile_result,
                    reason_code="management_lease_release_failed",
                )
            results.append(profile_result)
            if profile_result.changed and applied_action == "propose_canary":
                canary_proposed = True
        return self._management_report(
            tuple(results),
            now=moment,
            scheduled=scheduled,
        )

    def plan_management_reconciliation(
        self,
        *,
        now: datetime | None = None,
    ) -> ManagementPlanPreview:
        """Prepare the first actionable profile without mutating durable state."""
        moment = _management_utc_now(now)
        inputs = self._management_inputs(now=moment, scheduled=False)
        if isinstance(inputs, ManagementReconcileReport):
            plan_id = _checksum(
                _canonical_json({
                    "kind": "management-hold-plan-v1",
                    "reason_code": inputs.reason_code,
                    "planned_at": inputs.reconciled_at,
                })
            )
            return ManagementPlanPreview(
                plan_id=plan_id,
                reason_code=inputs.reason_code,
                profile_ids=(),
                planned_at=inputs.reconciled_at,
            )
        config, snapshot, pack = inputs
        held_reason = "no_change"
        for profile_id in sorted(config.profiles):
            prepared = self._prepare_management_profile(
                config=config,
                profile_id=profile_id,
                snapshot=snapshot,
                pack=pack,
                now=moment,
            )
            if isinstance(prepared, ManagementProfileReconcileResult):
                held_reason = prepared.reason_code
                continue
            plan_id = _checksum(
                _canonical_json({
                    "kind": "management-plan-preview-v1",
                    "revision_id": prepared.revision.revision_id,
                    "expected_authority_id": prepared.expected_authority_id,
                    "planned_at": _management_timestamp(moment),
                })
            )
            self._management_plans[plan_id] = prepared
            return ManagementPlanPreview(
                plan_id=plan_id,
                reason_code="mutation_planned",
                profile_ids=(profile_id,),
                planned_at=_management_timestamp(moment),
            )
        plan_id = _checksum(
            _canonical_json({
                "kind": "management-empty-plan-v1",
                "reason_code": held_reason,
                "planned_at": _management_timestamp(moment),
            })
        )
        return ManagementPlanPreview(
            plan_id=plan_id,
            reason_code=held_reason,
            profile_ids=(),
            planned_at=_management_timestamp(moment),
        )

    def apply_management_plan(
        self,
        plan_id: str,
        *,
        now: datetime | None = None,
    ) -> ManagementReconcileReport:
        """Apply one exact in-memory preview; stale user authority always wins."""
        moment = _management_utc_now(now)
        prepared = self._management_plans.pop(plan_id, None)
        if prepared is None:
            return ManagementReconcileReport.hold(
                "management_plan_missing", now=moment
            )
        authority_id = prepared.revision.management_authority_id
        owner_id = _checksum(
            _canonical_json({
                "kind": "management-plan-owner-v1",
                "plan_id": plan_id,
                "nonce": uuid.uuid4().hex,
            })
        )
        try:
            lease = self.store.acquire_management_lease(
                authority_id,
                prepared.profile_id,
                owner_id,
                moment,
                60.0,
            )
        except Exception:
            lease = None
        if lease is None:
            try:
                control = self.store.read_management_control(authority_id)
                reason_code = (
                    "management_frozen"
                    if control.frozen
                    else "management_lease_unavailable"
                )
            except Exception:
                reason_code = "management_lease_unavailable"
            return ManagementReconcileReport.hold(
                reason_code, now=moment
            )
        try:
            refreshed = self._refresh_prepared_management_plan(
                prepared,
                now=moment,
            )
            if isinstance(refreshed, ManagementProfileReconcileResult):
                result = refreshed
            else:
                result = self._apply_prepared_management_plan(
                    refreshed,
                    now=moment,
                )
        finally:
            try:
                released = self.store.release_management_lease(lease)
            except Exception:
                released = False
        if not released:
            result = replace(
                result,
                reason_code="management_lease_release_failed",
            )
        return self._management_report((result,), now=moment, scheduled=False)

    def _management_recovery_precondition(
        self,
        *,
        config: AutoRoutingConfig,
        receipt_id: str,
    ) -> tuple[dict[str, Any], ManagementConfigReceipt]:
        """Bind one exact incomplete receipt and its local recovery bytes."""
        receipt = self.store.read_management_receipt(receipt_id)
        if receipt is None:
            raise AutoRoutingServiceError("management recovery receipt is unavailable")
        revision = self.store.read_management_revision(receipt.revision_id)
        if revision is None:
            raise AutoRoutingServiceError("management recovery revision is unavailable")
        finalization = None
        if receipt.phase == "committed":
            finalization = (
                self.store.read_management_lifecycle_finalization_for_receipt(
                    receipt.receipt_id
                )
            )
            if finalization is None or finalization.phase == "finalized":
                raise AutoRoutingServiceError(
                    "management receipt is already committed and needs no recovery"
                )
        if receipt.phase == "recovery_required" and management_config_recovery_complete(
            receipt=receipt,
            revision=revision,
            store=self.store,
        ):
            raise AutoRoutingServiceError(
                "management receipt is already recovered and needs no recovery"
            )
        management_authority_id = management_authority_revision(config)
        if revision.management_authority_id != management_authority_id:
            raise AutoRoutingServiceError("management recovery authority changed")
        control = self.store.read_management_control(management_authority_id)
        if not control.frozen:
            raise AutoRoutingServiceError(
                "management must be frozen before recovery preview"
            )
        try:
            config_sha256 = hashlib.sha256(self.config_path.read_bytes()).hexdigest()
        except OSError as error:
            raise AutoRoutingServiceError(
                "management recovery config is unavailable"
            ) from error
        if finalization is not None:
            if authority_revision(config) != receipt.resulting_authority_id:
                raise AutoRoutingServiceError(
                    "management lifecycle recovery authority changed"
                )
            return (
                {
                    "authority_id": authority_revision(config),
                    "management_authority_id": management_authority_id,
                    "control_generation": control.generation,
                    "frozen": control.frozen,
                    "action": "recover",
                    "receipt": receipt.model_dump(
                        mode="json",
                        by_alias=True,
                        warnings=False,
                    ),
                    "finalization": finalization.model_dump(
                        mode="json",
                        by_alias=True,
                        warnings=False,
                    ),
                    "config_sha256": config_sha256,
                },
                receipt,
            )
        backup_path = _management_backup_path(self.config_path, receipt.receipt_id)
        try:
            backup_sha256 = hashlib.sha256(backup_path.read_bytes()).hexdigest()
        except OSError as error:
            raise AutoRoutingServiceError(
                "management recovery backup is unavailable"
            ) from error
        if not hmac.compare_digest(backup_sha256, receipt.backup_checksum):
            raise AutoRoutingServiceError("management recovery backup changed")
        return (
            {
                "authority_id": authority_revision(config),
                "management_authority_id": management_authority_id,
                "control_generation": control.generation,
                "frozen": control.frozen,
                "action": "recover",
                "receipt": receipt.model_dump(
                    mode="json",
                    by_alias=True,
                    warnings=False,
                ),
                "config_sha256": config_sha256,
                "backup_sha256": backup_sha256,
            },
            receipt,
        )

    def preview_management_recovery(self, receipt_id: str) -> dict[str, Any]:
        """Preview exact receipt-bound config recovery without changing state."""
        with profile_config_lock(self.config_path):
            config = self._configured_authority()
            precondition, receipt = self._management_recovery_precondition(
                config=config,
                receipt_id=receipt_id,
            )
        return {
            "apply": False,
            "action": "recover",
            "receipt_id": receipt.receipt_id,
            "revision_id": receipt.revision_id,
            "precondition": precondition,
            "precondition_hash": _checksum(_canonical_json(precondition)),
        }

    def apply_management_recovery(
        self,
        receipt_id: str,
        *,
        expected_hash: str,
    ) -> dict[str, Any]:
        """Apply one approved receipt recovery with an in-lock exact recheck."""
        self._require_management_precondition_hash(expected_hash)
        receipt = self.store.read_management_receipt(receipt_id)
        if receipt is None:
            raise AutoRoutingServiceError("management recovery receipt is unavailable")

        if receipt.phase == "committed":
            with profile_config_lock(self.config_path):
                current = self._configured_authority()
                try:
                    precondition, locked_receipt = (
                        self._management_recovery_precondition(
                            config=current,
                            receipt_id=receipt_id,
                        )
                    )
                except AutoRoutingServiceError as error:
                    raise AutoRoutingServiceError(
                        "management recovery precondition changed; run the preview again"
                    ) from error
                actual_hash = _checksum(_canonical_json(precondition))
                if not hmac.compare_digest(actual_hash, expected_hash):
                    raise AutoRoutingServiceError(
                        "management recovery precondition changed; run the preview again"
                    )
                finalization = (
                    self.store.read_management_lifecycle_finalization_for_receipt(
                        locked_receipt.receipt_id
                    )
                )
                if finalization is None or finalization.phase != "pending":
                    raise AutoRoutingServiceError(
                        "management recovery finalization changed; run the preview again"
                    )
                self._finalize_management_lifecycle(
                    finalization,
                    moment=_management_utc_now(None),
                    config_locked=True,
                )
            return {
                "apply": True,
                "action": "recover",
                "receipt_id": receipt.receipt_id,
                "revision_id": receipt.revision_id,
                "changed": False,
                "reason_code": "lifecycle_finalized",
                "applied_precondition_hash": expected_hash,
            }

        def check_approved_precondition(
            current: AutoRoutingConfig,
            locked_receipt: ManagementConfigReceipt,
        ) -> str | None:
            if locked_receipt.receipt_id != receipt_id:
                return "management recovery precondition changed; run the preview again"
            try:
                precondition, _receipt = self._management_recovery_precondition(
                    config=current,
                    receipt_id=receipt_id,
                )
            except AutoRoutingServiceError:
                return "management recovery precondition changed; run the preview again"
            actual_hash = _checksum(_canonical_json(precondition))
            if not hmac.compare_digest(actual_hash, expected_hash):
                return "management recovery precondition changed; run the preview again"
            return None

        try:
            result = recover_management_config_revision(
                receipt=receipt,
                store=self.store,
                config_path=self.config_path,
                precommit_check=check_approved_precondition,
            )
        except ConfigConflict as error:
            raise AutoRoutingServiceError(str(error)) from error
        return {
            "apply": True,
            "action": "recover",
            "receipt_id": receipt.receipt_id,
            "revision_id": result.revision_id,
            "changed": result.changed,
            "reason_code": result.reason_code,
            "applied_precondition_hash": expected_hash,
        }

    def recover_management(self) -> str:
        """Recover every incomplete local management receipt without discovery I/O."""
        recovered_finalization = False
        for finalization in (
            self.store.list_pending_management_lifecycle_finalizations()
        ):
            self._finalize_management_lifecycle(
                finalization,
                moment=_management_utc_now(None),
            )
            recovered_finalization = True
        rows = self.store.connection.execute(
            "SELECT receipt_id FROM management_config_receipts "
            "WHERE phase!='committed' ORDER BY created_at, receipt_id"
        ).fetchall()
        if not rows:
            return "recovered" if recovered_finalization else "no_recovery_required"
        results: list[ManagementRevisionResult] = []
        for row in rows:
            receipt = self.store.read_management_receipt(str(row["receipt_id"]))
            if receipt is None:
                raise AutoRoutingServiceError(
                    "management recovery receipt is unavailable"
                )
            revision = self.store.read_management_revision(receipt.revision_id)
            if revision is None:
                raise AutoRoutingServiceError(
                    "management recovery revision is unavailable"
                )
            if receipt.phase == "recovery_required" and (
                management_config_recovery_complete(
                    receipt=receipt,
                    revision=revision,
                    store=self.store,
                )
            ):
                continue
            results.append(
                recover_management_config_revision(
                    receipt=receipt,
                    store=self.store,
                    config_path=self.config_path,
                )
            )
        if not results:
            return "recovered" if recovered_finalization else "no_recovery_required"
        if any(result.reason_code == "authority_changed" for result in results):
            return "authority_changed"
        return "recovered"

    @staticmethod
    def _management_boundary_is_eligible(request: AgentRuntimeRequest) -> bool:
        """Admit only a new model-owned routing boundary to management."""
        context = request.context
        metadata = context.metadata
        if context.scope not in {"fresh_session", "delegation"}:
            return False
        if context.is_resume or context.manual_runtime_pin:
            return False
        excluded_markers = (
            "is_compression",
            "fixed_delegation_provider",
            "fixed_delegation_model",
            "recorded_replay",
            "recovered",
            "recovery",
            "active_session",
        )
        return not any(bool(metadata.get(marker)) for marker in excluded_markers)

    @staticmethod
    def _management_revision_patch(
        revision: ManagementRevision,
        profile_id: str,
    ) -> Any | None:
        return next(
            (patch for patch in revision.patches if patch.profile_id == profile_id),
            None,
        )

    def _management_target_id(
        self,
        *,
        state: ManagementProfileState,
        arm: str,
    ) -> str | None:
        """Resolve one management arm to the exact revision-recorded runtime."""
        if (
            state.control_revision_id is None
            or state.challenger_revision_id is None
        ):
            return None
        control = self.store.read_management_revision(state.control_revision_id)
        challenger = self.store.read_management_revision(
            state.challenger_revision_id
        )
        if (
            control is None
            or challenger is None
            or challenger.parent_revision_id != control.revision_id
            or challenger.action != "propose_canary"
        ):
            return None
        control_patch = self._management_revision_patch(control, state.profile_id)
        challenger_patch = self._management_revision_patch(
            challenger, state.profile_id
        )
        if control_patch is None or challenger_patch is None:
            return None
        if arm == "control":
            return control_patch.after_runtime_ids[0]
        ranked_target = (
            challenger.runtime_scores[0][0] if challenger.runtime_scores else None
        )
        if (
            ranked_target is not None
            and ranked_target in challenger_patch.after_runtime_ids
            and ranked_target != control_patch.after_runtime_ids[0]
        ):
            return ranked_target
        introduced = tuple(
            runtime_id
            for runtime_id in challenger_patch.after_runtime_ids
            if runtime_id not in set(challenger_patch.before_runtime_ids)
        )
        if len(introduced) != 1:
            return None
        return introduced[0]

    @staticmethod
    def _management_target_selection(
        *,
        selection: SelectionResult,
        profile: RouteProfile,
        inventory: InventorySnapshot,
        runtime_id: str,
    ) -> SelectionResult | None:
        target = next(
            (
                item
                for item in (*profile.primary_choices(), *profile.fallbacks)
                if item.runtime.stable_id() == runtime_id
            ),
            None,
        )
        runtime = next(
            (
                item
                for item in inventory.runtimes
                if item.key.stable_id() == runtime_id and item.state == "verified"
            ),
            None,
        )
        if target is None or runtime is None:
            return None
        selected_effort = selection.selected_reasoning_effort
        effort_positions = {
            effort: index for index, effort in enumerate(REASONING_EFFORT_ORDER)
        }
        if (
            selected_effort not in effort_positions
            or not (
                effort_positions[target.reasoning.minimum]
                <= effort_positions[selected_effort]
                <= effort_positions[target.reasoning.maximum]
            )
            or (
                target.supported_reasoning_efforts
                and selected_effort not in target.supported_reasoning_efforts
            )
        ):
            return None
        eligible = next(
            (
                candidate
                for candidate in selection.candidates
                if candidate.profile_id == profile.profile_id
                and candidate.runtime_id == runtime_id
                and candidate.eligible
            ),
            None,
        )
        if eligible is None:
            return None
        return replace(
            selection,
            selected_profile_id=profile.profile_id,
            selected_runtime=runtime,
        )

    def _resolve_exact_management_selection(
        self,
        *,
        selection: SelectionResult,
        hermes_config: Mapping[str, Any] | None,
    ) -> Any:
        """Resolve one exact management route without a fallback side path."""
        resolved = self.adapter.resolve(selection.selected_runtime.key)
        return self.adapter.to_agent_runtime_spec(
            resolved,
            reasoning_effort=selection.selected_reasoning_effort,
            hermes_config=hermes_config,
        )

    def _reserve_management_assignment(
        self,
        *,
        request: AgentRuntimeRequest,
        selected: SelectionResult,
        state: ManagementProfileState,
        arm: str,
        now: datetime,
    ) -> ManagementDecisionSnapshot:
        """Reserve and finalize the exact selected management arm."""
        if (
            state.control_revision_id is None
            or state.challenger_revision_id is None
            or selected.selected_profile_id != state.profile_id
        ):
            return ManagementDecisionSnapshot()
        operation_hash = operation_identity_hash(
            scope=request.context.scope,
            session_id=request.context.session_id,
            task_id=request.context.task_id,
            operation_id=request.context.operation_id,
            task_index=request.context.task_index,
        )
        selected_revision_id = (
            state.challenger_revision_id
            if arm == "challenger"
            else state.control_revision_id
        )
        seed = _canonical_json({
            "management_authority_id": state.management_authority_id,
            "profile_id": state.profile_id,
            "operation_identity_hash": operation_hash,
            "control_revision_id": state.control_revision_id,
            "challenger_revision_id": state.challenger_revision_id,
            "arm": arm,
        })
        reservation = self.store.reserve_management_assignment(
            ManagementCanaryAssignment(
                assignment_id=_checksum(seed),
                management_authority_id=state.management_authority_id,
                profile_id=state.profile_id,
                operation_identity_hash=operation_hash,
                control_revision_id=state.control_revision_id,
                challenger_revision_id=state.challenger_revision_id,
                arm=arm,
                created_at=_management_timestamp(now),
            ),
            expected_generation=state.generation,
        )
        try:
            finalized = self.store.finalize_management_assignment(
                assignment_id=reservation.assignment_id,
                runtime_id=selected.selected_runtime.key.stable_id(),
                reasoning_effort=selected.selected_reasoning_effort,
                expected_generation=state.generation,
            )
        except Exception:
            try:
                discarded = self.store.discard_management_reservation(
                    reservation.assignment_id,
                    expected_generation=state.generation,
                    expected_management_authority_id=(
                        reservation.management_authority_id
                    ),
                    expected_profile_id=reservation.profile_id,
                    expected_operation_identity_hash=(
                        reservation.operation_identity_hash
                    ),
                )
            except Exception:
                discarded = False
            if not discarded:
                challenger = self.store.read_management_revision(
                    state.challenger_revision_id
                )
                if challenger is not None:
                    _freeze_management_recovery(
                        revision=challenger,
                        store=self.store,
                    )
            raise
        return ManagementDecisionSnapshot(
            management_revision_id=selected_revision_id,
            management_assignment_id=finalized.assignment_id,
            management_profile_snapshot={state.profile_id: selected_revision_id},
        )

    def _apply_management_runtime_overlay(
        self,
        *,
        request: AgentRuntimeRequest,
        config: AutoRoutingConfig,
        selection: SelectionResult,
        inventory: InventorySnapshot,
        projected_spec: Any,
        adaptive_assignment_id: str | None,
        now: datetime,
    ) -> tuple[SelectionResult, Any, ManagementDecisionSnapshot]:
        """Resolve and persist one separate management arm, or keep control."""
        empty = ManagementDecisionSnapshot()
        profile_id = selection.selected_profile_id
        if (
            config.activation.mode != "active"
            or not config.autonomous_profile_management.enabled
            or not self._management_boundary_is_eligible(request)
            or profile_id is None
            or adaptive_assignment_id is not None
        ):
            return selection, projected_spec, empty
        profile = config.profiles.get(profile_id)
        if profile is None:
            return selection, projected_spec, empty
        management_authority_id = management_authority_revision(config)
        try:
            if self.store.list_pending_management_lifecycle_finalizations():
                return selection, projected_spec, empty
            control = self.store.read_management_control(management_authority_id)
            state = self.store.read_management_profile_state(
                management_authority_id,
                profile_id,
                current_authority_id=authority_revision(config),
            )
        except Exception:
            return selection, projected_spec, empty
        challenger_revision = (
            None
            if state.challenger_revision_id is None
            else self.store.read_management_revision(
                state.challenger_revision_id
            )
        )
        if (
            control.frozen
            or state.experiment_phase != "canary"
            or state.control_revision_id is None
            or state.challenger_revision_id is None
            or state.active_revision_id != state.control_revision_id
            or challenger_revision is None
            or challenger_revision.resulting_authority_id
            != authority_revision(config)
        ):
            return selection, projected_spec, empty

        control_runtime_id = self._management_target_id(state=state, arm="control")
        if control_runtime_id is None:
            return selection, projected_spec, empty
        control_selection = self._management_target_selection(
            selection=selection,
            profile=profile,
            inventory=inventory,
            runtime_id=control_runtime_id,
        )
        if control_selection is None:
            return selection, projected_spec, empty

        operation_hash = operation_identity_hash(
            scope=request.context.scope,
            session_id=request.context.session_id,
            task_id=request.context.task_id,
            operation_id=request.context.operation_id,
            task_index=request.context.task_index,
        )
        try:
            key = ensure_profile_canary_key(
                self.hermes_home,
                config_path=self.config_path,
            )
            arm = deterministic_canary_arm(
                key,
                profile_id,
                operation_hash,
                config.autonomous_profile_management.canary_fraction,
            )
        except Exception:
            try:
                control_spec = self._resolve_exact_management_selection(
                    selection=control_selection,
                    hermes_config=self._runtime_root_config(),
                )
            except Exception:
                return selection, projected_spec, empty
            return control_selection, control_spec, empty
        if arm == "control":
            target_runtime_id = control_runtime_id
            target_selection = control_selection
        else:
            target_runtime_id = self._management_target_id(state=state, arm=arm)
            target_selection = (
                None
                if target_runtime_id is None
                else self._management_target_selection(
                    selection=selection,
                    profile=profile,
                    inventory=inventory,
                    runtime_id=target_runtime_id,
                )
            )
        if target_selection is None:
            try:
                control_spec = self._resolve_exact_management_selection(
                    selection=control_selection,
                    hermes_config=self._runtime_root_config(),
                )
            except Exception:
                return selection, projected_spec, empty
            return control_selection, control_spec, empty
        try:
            target_spec = self._resolve_exact_management_selection(
                selection=target_selection,
                hermes_config=self._runtime_root_config(),
            )
        except Exception:
            if arm == "challenger":
                self._apply_management_canary_transition(
                    profile=profile,
                    state=state,
                    challenger_revision=challenger_revision,
                    challenger_runtime_id=target_runtime_id,
                    action="rollback",
                    reason_code="resolver_failure",
                    moment=now,
                )
            try:
                control_spec = self._resolve_exact_management_selection(
                    selection=control_selection,
                    hermes_config=self._runtime_root_config(),
                )
            except Exception:
                return selection, projected_spec, empty
            return control_selection, control_spec, empty
        try:
            snapshot = self._reserve_management_assignment(
                request=request,
                selected=target_selection,
                state=state,
                arm=arm,
                now=now,
            )
        except Exception:
            if arm == "control":
                return control_selection, target_spec, empty
            try:
                control_spec = self._resolve_exact_management_selection(
                    selection=control_selection,
                    hermes_config=self._runtime_root_config(),
                )
            except Exception:
                return selection, projected_spec, empty
            return control_selection, control_spec, empty
        if snapshot.management_assignment_id is None:
            if arm == "control":
                return control_selection, target_spec, empty
            try:
                control_spec = self._resolve_exact_management_selection(
                    selection=control_selection,
                    hermes_config=self._runtime_root_config(),
                )
            except Exception:
                return selection, projected_spec, empty
            return control_selection, control_spec, empty
        return target_selection, target_spec, snapshot

    @staticmethod
    def _management_event(
        *,
        state: ManagementProfileState,
        revision_id: str | None,
        event_type: str,
        reason_code: str,
        created_at: str,
    ) -> ManagementLifecycleEvent:
        seed = _canonical_json({
            "kind": "management-lifecycle-v1",
            "management_authority_id": state.management_authority_id,
            "profile_id": state.profile_id,
            "revision_id": revision_id,
            "event_type": event_type,
            "reason_code": reason_code,
            "created_at": created_at,
            "generation": state.generation,
        })
        return ManagementLifecycleEvent(
            event_id=_checksum(seed),
            management_authority_id=state.management_authority_id,
            profile_id=state.profile_id,
            revision_id=revision_id,
            event_type=event_type,
            reason_code=reason_code,
            created_at=created_at,
        )

    def _start_management_canary(
        self,
        *,
        state: ManagementProfileState,
        moment: datetime,
    ) -> ManagementAdvance:
        challenger = (
            None
            if state.active_revision_id is None
            else self.store.read_management_revision(state.active_revision_id)
        )
        control = (
            None
            if challenger is None or challenger.parent_revision_id is None
            else self.store.read_management_revision(challenger.parent_revision_id)
        )
        if (
            control is None
            or challenger is None
            or challenger.action != "propose_canary"
            or challenger.preceding_authority_id != control.resulting_authority_id
            or self._management_target_id(
                state=state.model_copy(
                    update={
                        "control_revision_id": control.revision_id,
                        "challenger_revision_id": challenger.revision_id,
                        "experiment_phase": "validated",
                        "active_revision_id": control.revision_id,
                        "authority_id": control.resulting_authority_id,
                    }
                ),
                arm="challenger",
            )
            is None
        ):
            return ManagementAdvance("hold", "management_pair_unavailable")
        created_at = _management_timestamp(moment)
        validated_state = ManagementProfileState(
            management_authority_id=state.management_authority_id,
            profile_id=state.profile_id,
            authority_id=control.resulting_authority_id,
            active_revision_id=control.revision_id,
            management_epoch=challenger.management_epoch,
            control_revision_id=control.revision_id,
            challenger_revision_id=challenger.revision_id,
            experiment_phase="validated",
            rejection_count=state.rejection_count,
            generation=state.generation,
            updated_at=created_at,
        )
        validated = self.store.transition_management_profile_state(
            profile_id=state.profile_id,
            authority_id=state.management_authority_id,
            expected_generation=state.generation,
            state=validated_state,
            event=self._management_event(
                state=state,
                revision_id=challenger.revision_id,
                event_type="validated",
                reason_code="management_pair_validated",
                created_at=created_at,
            ),
        )
        canary_at = _management_timestamp(moment + timedelta(microseconds=1))
        canary = self.store.transition_management_profile_state(
            profile_id=state.profile_id,
            authority_id=state.management_authority_id,
            expected_generation=validated.generation,
            state=validated.model_copy(
                update={
                    "experiment_phase": "canary",
                    "updated_at": canary_at,
                }
            ),
            event=self._management_event(
                state=validated,
                revision_id=challenger.revision_id,
                event_type="canary",
                reason_code="management_canary_started",
                created_at=canary_at,
            ),
        )
        return ManagementAdvance(
            "canary",
            "management_canary_started",
            canary.challenger_revision_id,
        )

    def list_management_observations(
        self,
        *,
        management_authority_id: str,
        profile_id: str,
        revision_id: str,
    ) -> tuple[dict[str, Any], ...]:
        """Read only outcomes attributed to an exact management arm."""
        observations: list[dict[str, Any]] = []
        for event in self.store.list_evidence_events(profile_id=profile_id):
            if not event.is_initial_routing_task:
                continue
            decision = self.store.read_decision(event.decision_id)
            if (
                decision is None
                or decision.selected_profile_id != profile_id
                or decision.management_revision_id != revision_id
                or decision.management_assignment_id is None
            ):
                continue
            assignment = self.store.read_management_assignment(
                decision.management_assignment_id
            )
            if (
                assignment is None
                or assignment.management_authority_id
                != management_authority_id
                or assignment.profile_id != profile_id
                or assignment.phase not in {"finalized", "terminal"}
                or assignment.runtime_id != event.runtime_id
                or assignment.reasoning_effort != event.reasoning_effort
            ):
                continue
            expected_revision_id = (
                assignment.challenger_revision_id
                if assignment.arm == "challenger"
                else assignment.control_revision_id
            )
            if expected_revision_id != revision_id:
                continue
            observations.append({
                "evidence_id": event.evidence_id,
                "parent_evidence_id": event.parent_evidence_id,
                "decision_id": event.decision_id,
                "assignment_id": assignment.assignment_id,
                "is_initial_routing_task": True,
                "source": event.source,
                "outcome": event.outcome,
                "feedback_value": event.feedback_value,
                "retry_count": event.retry_count,
                "cost_usd": event.cost_usd,
                "latency_seconds": event.latency_seconds,
                "observed_at": event.observed_at,
            })
        return tuple(
            sorted(
                observations,
                key=lambda item: (str(item["observed_at"]), item["evidence_id"]),
            )
        )

    def _daily_management_experiment_spend_usd(
        self,
        management_authority_id: str,
        moment: datetime,
    ) -> float:
        day_start = moment.astimezone(UTC).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        ).isoformat().replace("+00:00", "Z")
        total = 0.0
        for event in self.store.list_evidence_events(
            observed_at_or_after=day_start
        ):
            if not event.is_initial_routing_task or event.source != "hermes_turn_outcome":
                continue
            decision = self.store.read_decision(event.decision_id)
            if decision is None or decision.management_assignment_id is None:
                continue
            assignment = self.store.read_management_assignment(
                decision.management_assignment_id
            )
            if (
                assignment is not None
                and assignment.management_authority_id == management_authority_id
            ):
                total += float(event.cost_usd)
        return total

    def _settle_management_canary(
        self,
        *,
        state: ManagementProfileState,
        event_type: str,
        reason_code: str,
        rejection_count: int,
        moment: datetime,
    ) -> ManagementProfileState:
        """Terminalize the old canary and return its admitted revision to eligible."""
        if state.challenger_revision_id is None:
            raise AutoRoutingServiceError("management challenger is unavailable")
        challenger = self.store.read_management_revision(
            state.challenger_revision_id
        )
        if challenger is None:
            raise AutoRoutingServiceError("management challenger is unavailable")
        settled_at = _management_timestamp(moment)
        cooldown = self.store.transition_management_profile_state(
            profile_id=state.profile_id,
            authority_id=state.management_authority_id,
            expected_generation=state.generation,
            state=state.model_copy(
                update={
                    "authority_id": challenger.resulting_authority_id,
                    "active_revision_id": challenger.revision_id,
                    "experiment_phase": "cooldown",
                    "cooldown_until": settled_at,
                    "rejection_count": rejection_count,
                    "updated_at": settled_at,
                }
            ),
            event=self._management_event(
                state=state,
                revision_id=challenger.revision_id,
                event_type=event_type,
                reason_code=reason_code,
                created_at=settled_at,
            ),
        )
        eligible_at = _management_timestamp(moment + timedelta(microseconds=1))
        return self.store.transition_management_profile_state(
            profile_id=state.profile_id,
            authority_id=state.management_authority_id,
            expected_generation=cooldown.generation,
            state=cooldown.model_copy(
                update={
                    "control_revision_id": None,
                    "challenger_revision_id": None,
                    "experiment_phase": "eligible",
                    "cooldown_until": None,
                    "updated_at": eligible_at,
                }
            ),
            event=self._management_event(
                state=cooldown,
                revision_id=challenger.revision_id,
                event_type="cooldown",
                reason_code="management_revision_settled",
                created_at=eligible_at,
            ),
        )

    @staticmethod
    def _management_runtime_ids(profile: RouteProfile) -> tuple[str, ...]:
        return tuple(
            target.runtime.stable_id()
            for target in (*profile.primary_choices(), *profile.fallbacks)
        )

    def _management_backup_authority(
        self,
        revision: ManagementRevision,
    ) -> AutoRoutingConfig:
        row = self.store.connection.execute(
            "SELECT receipt_id FROM management_config_receipts "
            "WHERE revision_id=? AND phase='committed'",
            (revision.revision_id,),
        ).fetchone()
        if row is None:
            raise AutoRoutingServiceError("management rollback receipt is unavailable")
        receipt = self.store.read_management_receipt(str(row["receipt_id"]))
        if receipt is None:
            raise AutoRoutingServiceError("management rollback receipt is unavailable")
        backup_path = _management_backup_path(self.config_path, receipt.receipt_id)
        backup_bytes = backup_path.read_bytes()
        if hashlib.sha256(backup_bytes).hexdigest() != receipt.backup_checksum:
            raise AutoRoutingServiceError("management rollback backup changed")
        document = fast_safe_load(backup_bytes)
        if not isinstance(document, Mapping):
            raise AutoRoutingServiceError("management rollback backup is invalid")
        proposal = parse_config(document)
        if (
            authority_revision(proposal) != receipt.preceding_authority_id
            or management_authority_revision(proposal)
            != revision.management_authority_id
        ):
            raise AutoRoutingServiceError("management rollback authority changed")
        return proposal

    def _management_lifecycle_revision(
        self,
        *,
        current: AutoRoutingConfig,
        proposal: AutoRoutingConfig,
        parent: ManagementRevision,
        profile_id: str,
        action: str,
        reason_code: str,
        moment: datetime,
    ) -> ManagementRevision:
        created_at = _management_timestamp(moment + timedelta(microseconds=2))
        patches = self._management_profile_patches(
            before=current,
            after=proposal,
            changed_profile_id=profile_id,
            reason_code=reason_code,
        )
        seed = {
            "kind": "management-lifecycle-revision-v1",
            "preceding_authority_id": authority_revision(current),
            "resulting_authority_id": authority_revision(proposal),
            "management_authority_id": parent.management_authority_id,
            "parent_revision_id": parent.revision_id,
            "management_epoch": parent.management_epoch + 1,
            "action": action,
            "patches": [patch.model_dump(mode="json") for patch in patches],
            "created_at": created_at,
        }
        return ManagementRevision(
            revision_id=_checksum(_canonical_json(seed)),
            preceding_authority_id=authority_revision(current),
            resulting_authority_id=authority_revision(proposal),
            management_authority_id=parent.management_authority_id,
            parent_revision_id=parent.revision_id,
            ranking_pack=parent.ranking_pack,
            inventory_revision=parent.inventory_revision,
            inventory_fingerprint=parent.inventory_fingerprint,
            management_epoch=parent.management_epoch + 1,
            action=action,
            patches=patches,
            runtime_scores=parent.runtime_scores if action == "promote" else (),
            created_at=created_at,
        )

    def _promoted_management_authority(
        self,
        *,
        config: AutoRoutingConfig,
        profile_id: str,
        challenger_runtime_id: str,
    ) -> AutoRoutingConfig:
        profile = config.profiles[profile_id]
        target = next(
            (
                item
                for item in (*profile.primary_choices(), *profile.fallbacks)
                if item.runtime.stable_id() == challenger_runtime_id
            ),
            None,
        )
        if target is None:
            raise AutoRoutingServiceError("management challenger target is unavailable")
        promoted = target.model_copy(update={"revision_status": "active"})
        challengers = tuple(
            item.model_copy(update={"revision_status": "challenger"})
            for item in profile.primary_choices()
            if item.runtime.stable_id() != challenger_runtime_id
        )
        fallbacks = tuple(
            item
            for item in profile.fallbacks
            if item.runtime.stable_id() != challenger_runtime_id
        )
        updated = profile.model_copy(
            update={
                "primary": promoted,
                "primary_challengers": challengers,
                "fallbacks": fallbacks,
            }
        )
        return config.model_copy(
            update={"profiles": {**config.profiles, profile_id: updated}}
        )

    def _apply_management_lifecycle_revision(
        self,
        *,
        proposal: AutoRoutingConfig,
        revision: ManagementRevision,
        finalization: ManagementLifecycleFinalization,
        moment: datetime,
    ) -> ManagementRevisionResult:
        control = self.store.read_management_control(
            revision.management_authority_id
        )
        current = self._configured_authority()
        activation_rollover = self._management_activation_rollover(
            current=current,
            proposal=proposal,
            revision=revision,
        )
        return apply_management_config_revision(
            proposal=proposal,
            revision=revision,
            expected_authority_id=revision.preceding_authority_id,
            admission_utc_day=moment.astimezone(UTC).date().isoformat(),
            store=self.store,
            config_path=self.config_path,
            expected_control_generation=control.generation,
            activation_rollover=activation_rollover,
            defer_profile_state_commit=True,
            lifecycle_finalization=finalization,
        )

    def _freeze_management_lifecycle_finalization(
        self,
        finalization: ManagementLifecycleFinalization,
        *,
        moment: datetime,
    ) -> None:
        """Freeze new work without destroying the exact recoverable canary state."""
        control = self.store.read_management_control(
            finalization.management_authority_id
        )
        if control.frozen:
            return
        state = self.store.read_management_profile_state(
            finalization.management_authority_id,
            finalization.profile_id,
        )
        frozen_at = _management_timestamp(moment)
        self.store.transition_management_control(
            control=control.model_copy(
                update={"frozen": True, "updated_at": frozen_at}
            ),
            expected_generation=control.generation,
            event=self._management_event(
                state=state,
                revision_id=None,
                event_type="frozen",
                reason_code="management_state_settlement_failed",
                created_at=frozen_at,
            ),
        )

    def _finalize_management_lifecycle(
        self,
        finalization: ManagementLifecycleFinalization,
        *,
        moment: datetime,
        config_locked: bool = False,
    ) -> ManagementProfileState:
        """Idempotently settle one exact committed lifecycle revision."""
        lock = nullcontext() if config_locked else profile_config_lock(self.config_path)
        with lock:
            config = self._configured_authority()
            revision = self.store.read_management_revision(
                finalization.revision_id
            )
            challenger = self.store.read_management_revision(
                finalization.challenger_revision_id
            )
            receipt = self.store.read_management_receipt(finalization.receipt_id)
            stored = self.store.read_management_lifecycle_finalization(
                finalization.finalization_id
            )
            stored_matches = stored is not None and stored == finalization.model_copy(
                update={"phase": stored.phase, "updated_at": stored.updated_at}
            )
            if (
                revision is None
                or challenger is None
                or receipt is None
                or not stored_matches
                or receipt.phase != "committed"
                or receipt.revision_id != revision.revision_id
                or authority_revision(config) != revision.resulting_authority_id
                or management_authority_revision(config)
                != finalization.management_authority_id
            ):
                raise AutoRoutingServiceError(
                    "management lifecycle finalization authority is ambiguous"
                )
            finalization = stored
            state = self.store.read_management_profile_state(
                finalization.management_authority_id,
                finalization.profile_id,
            )
            if finalization.phase == "finalized":
                expected_phase = (
                    "eligible" if finalization.action == "promote" else "cooldown"
                )
                if (
                    state.active_revision_id != revision.revision_id
                    or state.authority_id != revision.resulting_authority_id
                    or state.experiment_phase != expected_phase
                ):
                    raise AutoRoutingServiceError(
                        "finalized management lifecycle state is ambiguous"
                    )
                return state
            if (
                state.generation != finalization.expected_state_generation
                or state.experiment_phase != "canary"
                or state.challenger_revision_id != challenger.revision_id
                or state.management_authority_id
                != finalization.management_authority_id
                or state.profile_id != finalization.profile_id
            ):
                raise AutoRoutingServiceError(
                    "pending management lifecycle state is ambiguous"
                )
            with self.store.write_txn():
                settlement_moment = _parse_timestamp(finalization.settlement_at)
                self._settle_management_canary(
                    state=state,
                    event_type=finalization.event_type,
                    reason_code=finalization.reason_code,
                    rejection_count=finalization.rejection_count,
                    moment=settlement_moment,
                )
                if finalization.action == "rollback":
                    settled = self._finish_management_rollback_cooldown(
                        revision=revision,
                        rejection_count=finalization.rejection_count,
                        moment=settlement_moment,
                    )
                else:
                    settled = self._activate_management_lifecycle_revision(
                        revision=revision,
                        rejection_count=finalization.rejection_count,
                        moment=settlement_moment,
                    )
                self.store.finalize_management_lifecycle_finalization(
                    finalization.finalization_id,
                    expected_phase="pending",
                    updated_at=_management_timestamp(
                        settlement_moment + timedelta(microseconds=5)
                    ),
                )
            return settled

    def _activate_management_lifecycle_revision(
        self,
        *,
        revision: ManagementRevision,
        rejection_count: int,
        moment: datetime,
    ) -> ManagementProfileState:
        profile_id = revision.patches[0].profile_id
        state = self.store.read_management_profile_state(
            revision.management_authority_id,
            profile_id,
        )
        activated_at = _management_timestamp(moment + timedelta(microseconds=3))
        return self.store.transition_management_profile_state(
            profile_id=profile_id,
            authority_id=revision.management_authority_id,
            expected_generation=state.generation,
            state=state.model_copy(
                update={
                    "authority_id": revision.resulting_authority_id,
                    "active_revision_id": revision.revision_id,
                    "management_epoch": revision.management_epoch,
                    "experiment_phase": "eligible",
                    "rejection_count": rejection_count,
                    "updated_at": activated_at,
                }
            ),
            event=self._management_event(
                state=state,
                revision_id=revision.revision_id,
                event_type="proposed",
                reason_code=revision.patches[0].reason_codes[0],
                created_at=activated_at,
            ),
        )

    def _finish_management_rollback_cooldown(
        self,
        *,
        revision: ManagementRevision,
        rejection_count: int,
        moment: datetime,
    ) -> ManagementProfileState:
        state = self.store.read_management_profile_state(
            revision.management_authority_id,
            revision.patches[0].profile_id,
        )
        parent = self.store.read_management_revision(
            revision.parent_revision_id or ""
        )
        if parent is None:
            raise AutoRoutingServiceError("management rollback parent is unavailable")
        validated_at = _management_timestamp(moment + timedelta(microseconds=3))
        validated = self.store.transition_management_profile_state(
            profile_id=state.profile_id,
            authority_id=state.management_authority_id,
            expected_generation=state.generation,
            state=state.model_copy(
                update={
                    "authority_id": parent.resulting_authority_id,
                    "active_revision_id": parent.revision_id,
                    "management_epoch": revision.management_epoch,
                    "control_revision_id": parent.revision_id,
                    "challenger_revision_id": revision.revision_id,
                    "experiment_phase": "validated",
                    "rejection_count": rejection_count,
                    "updated_at": validated_at,
                }
            ),
            event=self._management_event(
                state=state,
                revision_id=revision.revision_id,
                event_type="validated",
                reason_code="rollback_cooldown_validated",
                created_at=validated_at,
            ),
        )
        policy = self._configured_authority().autonomous_profile_management
        cooldown_seconds = min(
            policy.cooldown_max_seconds,
            policy.cooldown_base_seconds * (2 ** max(0, rejection_count - 1)),
        )
        cooldown_until = moment + timedelta(seconds=cooldown_seconds)
        cooldown_at = _management_timestamp(moment + timedelta(microseconds=4))
        return self.store.transition_management_profile_state(
            profile_id=state.profile_id,
            authority_id=state.management_authority_id,
            expected_generation=validated.generation,
            state=validated.model_copy(
                update={
                    "authority_id": revision.resulting_authority_id,
                    "active_revision_id": revision.revision_id,
                    "experiment_phase": "cooldown",
                    "cooldown_until": _management_timestamp(cooldown_until),
                    "updated_at": cooldown_at,
                }
            ),
            event=self._management_event(
                state=validated,
                revision_id=revision.revision_id,
                event_type="cooldown",
                reason_code="management_rollback_cooldown",
                created_at=cooldown_at,
            ),
        )

    def _apply_management_canary_transition(
        self,
        *,
        profile: RouteProfile,
        state: ManagementProfileState,
        challenger_revision: ManagementRevision,
        challenger_runtime_id: str,
        action: str,
        reason_code: str,
        moment: datetime,
    ) -> ManagementAdvance:
        rejection_count = state.rejection_count + (1 if action == "rollback" else 0)
        try:
            current = self._configured_authority()
            if action == "promote":
                proposal = self._promoted_management_authority(
                    config=current,
                    profile_id=profile.profile_id,
                    challenger_runtime_id=challenger_runtime_id,
                )
                transition_reason = "management_challenger_promoted"
            else:
                backup = self._management_backup_authority(challenger_revision)
                if set(backup.profiles) != set(current.profiles):
                    raise AutoRoutingServiceError(
                        "management rollback profile topology changed"
                    )
                proposal = current.model_copy(
                    update={
                        "profiles": {
                            **current.profiles,
                            profile.profile_id: backup.profiles[profile.profile_id],
                        }
                    }
                )
                transition_reason = reason_code
            lineage_parent = self._latest_management_revision(
                challenger_revision.management_authority_id
            )
            if lineage_parent is None:
                raise AutoRoutingServiceError(
                    "management lifecycle parent is unavailable"
                )
            transition = self._management_lifecycle_revision(
                current=current,
                proposal=proposal,
                parent=lineage_parent,
                profile_id=profile.profile_id,
                action=action,
                reason_code=transition_reason,
                moment=moment,
            )
            finalization_seed = {
                "kind": "management-lifecycle-finalization-v1",
                "receipt_id": _management_receipt_id(transition.revision_id),
                "revision_id": transition.revision_id,
                "challenger_revision_id": challenger_revision.revision_id,
                "management_authority_id": transition.management_authority_id,
                "profile_id": profile.profile_id,
                "action": action,
                "expected_state_generation": state.generation,
                "settlement_at": _management_timestamp(moment),
            }
            finalization = ManagementLifecycleFinalization(
                finalization_id=_checksum(_canonical_json(finalization_seed)),
                receipt_id=_management_receipt_id(transition.revision_id),
                revision_id=transition.revision_id,
                challenger_revision_id=challenger_revision.revision_id,
                management_authority_id=transition.management_authority_id,
                profile_id=profile.profile_id,
                action=action,
                event_type="promoted" if action == "promote" else "rejected",
                reason_code=reason_code,
                rejection_count=rejection_count,
                expected_state_generation=state.generation,
                settlement_at=_management_timestamp(moment),
                phase="pending",
                created_at=transition.created_at,
                updated_at=transition.created_at,
            )
            result = self._apply_management_lifecycle_revision(
                proposal=proposal,
                revision=transition,
                finalization=finalization,
                moment=moment,
            )
        except BaseException:
            _freeze_management_recovery(
                revision=challenger_revision.model_copy(
                    update={"created_at": _management_timestamp(moment)}
                ),
                store=self.store,
            )
            return ManagementAdvance(
                "frozen",
                "management_rollback_failed",
                challenger_revision.revision_id,
            )
        if not result.changed:
            _freeze_management_recovery(
                revision=challenger_revision.model_copy(
                    update={"created_at": _management_timestamp(moment)}
                ),
                store=self.store,
            )
            return ManagementAdvance(
                "frozen",
                "management_rollback_failed",
                challenger_revision.revision_id,
            )
        try:
            self._finalize_management_lifecycle(
                finalization,
                moment=moment,
            )
        except BaseException:
            try:
                self._freeze_management_lifecycle_finalization(
                    finalization,
                    moment=moment + timedelta(microseconds=6),
                )
            except BaseException:
                pass
            return ManagementAdvance(
                "frozen",
                "management_state_settlement_failed",
                transition.revision_id,
            )
        return ManagementAdvance(action, transition_reason, transition.revision_id)

    def _evaluate_management_canary(
        self,
        *,
        config: AutoRoutingConfig,
        profile: RouteProfile,
        state: ManagementProfileState,
        moment: datetime,
    ) -> ManagementAdvance:
        if (
            state.control_revision_id is None
            or state.challenger_revision_id is None
        ):
            return ManagementAdvance("hold", "management_pair_unavailable")
        control_revision = self.store.read_management_revision(
            state.control_revision_id
        )
        challenger_revision = self.store.read_management_revision(
            state.challenger_revision_id
        )
        if control_revision is None or challenger_revision is None:
            return ManagementAdvance("hold", "management_pair_unavailable")
        control_runtime_id = self._management_target_id(state=state, arm="control")
        challenger_runtime_id = self._management_target_id(
            state=state, arm="challenger"
        )
        if control_runtime_id is None or challenger_runtime_id is None:
            return ManagementAdvance("hold", "management_target_unavailable")
        control_observations = self.list_management_observations(
            management_authority_id=state.management_authority_id,
            profile_id=profile.profile_id,
            revision_id=control_revision.revision_id,
        )
        challenger_observations = self.list_management_observations(
            management_authority_id=state.management_authority_id,
            profile_id=profile.profile_id,
            revision_id=challenger_revision.revision_id,
        )
        adverse = next(
            (
                item
                for item in challenger_observations
                if item["source"] == "user_feedback"
                and item["feedback_value"] in {"rejected", "corrected"}
            ),
            None,
        )
        guardrail_reason = self._operational_guardrail_reason(
            config=config,
            profile=profile,
            runtime_id=challenger_runtime_id,
            observations=challenger_observations,
            now=moment,
            experiment_spend_usd=self._daily_management_experiment_spend_usd(
                state.management_authority_id,
                moment,
            ),
        )
        control_quality = summarize_quality(control_observations)
        challenger_quality = summarize_quality(challenger_observations)
        regression = rollback_decision(
            (),
            assignment_id="management-aggregate",
            threshold=(
                config.autonomous_profile_management.observed_regression_threshold
            ),
            control=control_quality,
            challenger=challenger_quality,
            minimum_samples=(
                config.autonomous_profile_management.minimum_comparable_samples
            ),
        )
        promotion = promotion_decision(
            control_quality,
            challenger_quality,
            minimum_samples=(
                config.autonomous_profile_management.minimum_comparable_samples
            ),
            confidence_level=config.autonomous_profile_management.confidence_level,
        )
        rollback_reason = (
            "exact_assignment_feedback"
            if adverse is not None
            else guardrail_reason
            if guardrail_reason is not None
            else regression.reason
            if regression.action == "rollback"
            else None
        )
        action = "rollback" if rollback_reason is not None else promotion.action
        if action not in {"promote", "rollback"}:
            return ManagementAdvance("hold", promotion.reason)

        return self._apply_management_canary_transition(
            profile=profile,
            state=state,
            challenger_revision=challenger_revision,
            challenger_runtime_id=challenger_runtime_id,
            action=action,
            reason_code=(
                promotion.reason if action == "promote" else str(rollback_reason)
            ),
            moment=moment,
        )

    def maybe_advance_management(
        self,
        *,
        profile_id: str,
        now: datetime | None = None,
    ) -> ManagementAdvance:
        """Advance only the independent receipt-backed management lifecycle."""
        moment = _management_utc_now(now)
        config = self._configured_authority()
        if not config.autonomous_profile_management.enabled:
            return ManagementAdvance("disabled", "management_disabled")
        if self.store.list_pending_management_lifecycle_finalizations():
            return ManagementAdvance("hold", "management_recovery_required")
        profile = config.profiles.get(profile_id)
        if profile is None:
            return ManagementAdvance("hold", "profile_missing")
        management_authority_id = management_authority_revision(config)
        control = self.store.read_management_control(management_authority_id)
        if control.frozen:
            return ManagementAdvance("frozen", "management_frozen")
        state = self.store.read_management_profile_state(
            management_authority_id,
            profile_id,
            current_authority_id=authority_revision(config),
        )
        current_authority_id = authority_revision(config)
        if state.experiment_phase in {"validated", "canary"}:
            challenger = self.store.read_management_revision(
                state.challenger_revision_id or ""
            )
            if (
                challenger is None
                or challenger.resulting_authority_id != current_authority_id
            ):
                return ManagementAdvance("hold", "authority_changed")
        if state.experiment_phase == "recovery_required":
            return ManagementAdvance("hold", "management_recovery_required")
        if state.experiment_phase == "cooldown":
            if (
                state.cooldown_until is not None
                and _parse_timestamp(state.cooldown_until) > moment
            ):
                return ManagementAdvance(
                    "cooldown",
                    "cooldown_active",
                    state.active_revision_id,
                    max(
                        0.0,
                        (_parse_timestamp(state.cooldown_until) - moment).total_seconds(),
                    ),
                )
            if state.active_revision_id is None:
                return ManagementAdvance("hold", "management_revision_unavailable")
            created_at = _management_timestamp(moment)
            rolled_back = self.store.transition_management_profile_state(
                profile_id=profile_id,
                authority_id=management_authority_id,
                expected_generation=state.generation,
                state=state.model_copy(
                    update={
                        "control_revision_id": None,
                        "challenger_revision_id": None,
                        "experiment_phase": "rolled_back",
                        "cooldown_until": None,
                        "updated_at": created_at,
                    }
                ),
                event=self._management_event(
                    state=state,
                    revision_id=state.active_revision_id,
                    event_type="rolled_back",
                    reason_code="management_cooldown_complete",
                    created_at=created_at,
                ),
            )
            return ManagementAdvance(
                "rolled_back",
                "management_cooldown_complete",
                rolled_back.active_revision_id,
            )
        if state.experiment_phase == "rolled_back":
            created_at = _management_timestamp(moment)
            eligible = self.store.transition_management_profile_state(
                profile_id=profile_id,
                authority_id=management_authority_id,
                expected_generation=state.generation,
                state=state.model_copy(
                    update={
                        "experiment_phase": "eligible",
                        "updated_at": created_at,
                    }
                ),
                event=self._management_event(
                    state=state,
                    revision_id=state.active_revision_id,
                    event_type="cooldown",
                    reason_code="management_eligible",
                    created_at=created_at,
                ),
            )
            return ManagementAdvance(
                "eligible", "management_eligible", eligible.active_revision_id
            )
        if state.experiment_phase == "eligible":
            if state.active_revision_id is not None:
                active = self.store.read_management_revision(state.active_revision_id)
                if (
                    active is None
                    or active.resulting_authority_id != current_authority_id
                ):
                    return ManagementAdvance("hold", "authority_changed")
            concurrent = self.store.connection.execute(
                "SELECT profile_id FROM management_profile_states "
                "WHERE management_authority_id=? AND profile_id<>? "
                "AND experiment_phase IN ('validated', 'canary') LIMIT 1",
                (management_authority_id, profile_id),
            ).fetchone()
            if concurrent is not None:
                return ManagementAdvance("hold", "management_canary_active")
            return self._start_management_canary(state=state, moment=moment)
        if state.experiment_phase == "validated":
            return ManagementAdvance("hold", "management_validation_incomplete")
        if state.experiment_phase != "canary":
            return ManagementAdvance("hold", f"phase_{state.experiment_phase}")
        return self._evaluate_management_canary(
            config=config,
            profile=profile,
            state=state,
            moment=moment,
        )

    def record_management_outcome(
        self,
        outcome: object,
        *,
        now: datetime | None = None,
    ) -> ManagementAdvance:
        """Advance only an exact persisted management decision assignment."""
        hold = ManagementAdvance("hold", "management_outcome_unattributed")
        recorded = getattr(outcome, "event", outcome)
        decision_id = (
            recorded.get("decision_id")
            if isinstance(recorded, Mapping)
            else getattr(recorded, "decision_id", None)
        )
        if decision_id is None:
            return hold
        try:
            decision = self.store.read_decision(str(decision_id))
            if (
                decision is None
                or decision.selected_profile_id is None
                or decision.management_revision_id is None
                or decision.management_assignment_id is None
                or not decision.management_profile_snapshot
            ):
                return hold
            profile_id = decision.selected_profile_id
            selected_revision_id = decision.management_profile_snapshot.get(
                profile_id
            )
            if (
                selected_revision_id != decision.management_revision_id
                or dict(decision.management_profile_snapshot)
                != {profile_id: selected_revision_id}
            ):
                return hold
            revision = self.store.read_management_revision(selected_revision_id)
            assignment = self.store.read_management_assignment(
                decision.management_assignment_id
            )
            expected_revision_id = (
                None
                if assignment is None
                else assignment.challenger_revision_id
                if assignment.arm == "challenger"
                else assignment.control_revision_id
            )
            expected_operation_hash = operation_identity_hash(
                scope=decision.scope,
                session_id=decision.session_id,
                task_id=decision.task_id,
                operation_id=decision.operation_id,
                task_index=decision.task_index,
            )
            if (
                revision is None
                or self._management_revision_patch(revision, profile_id) is None
                or assignment is None
                or assignment.phase not in {"finalized", "terminal"}
                or assignment.assignment_id
                != decision.management_assignment_id
                or assignment.management_authority_id
                != revision.management_authority_id
                or assignment.profile_id != profile_id
                or assignment.operation_identity_hash
                != expected_operation_hash
                or expected_revision_id != selected_revision_id
                or assignment.runtime_id
                != decision.selected_runtime.stable_id()
                or assignment.reasoning_effort
                != decision.selected_reasoning_effort
            ):
                return hold
        except Exception:
            return hold
        return self.maybe_advance_management(
            profile_id=profile_id,
            now=now,
        )

    def _adaptation_control_context(
        self,
        profile_id: str,
    ) -> tuple[AutoRoutingConfig, RouteProfile, str, Any]:
        """Resolve one profile-local control without consulting runtime state."""
        self._assert_profile_isolation()
        config = self._configured_authority()
        profile = config.profiles.get(profile_id)
        if profile is None:
            raise AutoRoutingServiceError(
                f"auto-routing profile {profile_id!r} is unavailable"
            )
        authority_id = authority_revision(config)
        control = self.store.read_profile_control(authority_id, profile_id)
        return config, profile, authority_id, control

    @staticmethod
    def _profile_revision_checksum(revision: AdaptiveProfileRevision) -> str:
        return _checksum(
            _canonical_json(
                revision.model_dump(mode="json", by_alias=True, warnings=False)
            )
        )

    def adaptation_status(self, profile_id: str) -> dict[str, Any]:
        """Return read-only profile-local adaptive control state."""
        _config, profile, authority_id, control = self._adaptation_control_context(
            profile_id
        )
        effective_revision_id = (
            control.active_revision_id
            or self.static_profile_revision_id(authority_id, profile_id)
        )
        return {
            "authority_id": authority_id,
            "profile_id": profile_id,
            "enabled": profile.adaptation.enabled,
            "settings": profile.adaptation.model_dump(mode="json"),
            "active_revision_id": control.active_revision_id,
            "effective_revision_id": effective_revision_id,
            "control_revision_id": control.control_revision_id,
            "challenger_revision_id": control.challenger_revision_id,
            "experiment_phase": control.experiment_phase,
            "frozen": control.frozen,
            "cooldown_until": control.cooldown_until,
            "rejection_count": control.rejection_count,
            "generation": control.generation,
            "updated_at": control.updated_at,
        }

    def adaptation_history(self, profile_id: str) -> dict[str, Any]:
        """Return immutable content-free revision and lifecycle history."""
        _config, _profile, authority_id, _control = self._adaptation_control_context(
            profile_id
        )
        revisions = self.store.list_profile_revisions(authority_id, profile_id)
        events = self.store.list_adaptive_lifecycle_events(authority_id, profile_id)
        return {
            "authority_id": authority_id,
            "profile_id": profile_id,
            "revisions": [
                {
                    **revision.model_dump(
                        mode="json",
                        by_alias=True,
                        warnings=False,
                    ),
                    "checksum": self._profile_revision_checksum(revision),
                }
                for revision in revisions
            ],
            "events": [
                event.model_dump(mode="json", by_alias=True, warnings=False)
                for event in events
            ],
        }

    def _adaptation_control_precondition(
        self,
        *,
        action: str,
        profile_id: str,
        revision_id: str | None,
    ) -> tuple[dict[str, Any], Any]:
        config, profile, authority_id, control = self._adaptation_control_context(
            profile_id
        )
        if not profile.adaptation.enabled:
            raise AutoRoutingServiceError(
                f"adaptation is not enabled for profile {profile_id!r}"
            )
        requested: dict[str, Any]
        if action in {"freeze", "unfreeze"}:
            if revision_id is not None:
                raise AutoRoutingServiceError(
                    f"adapt {action} does not accept a revision"
                )
            requested = {"frozen": action == "freeze"}
        elif action == "rollback":
            if revision_id is None:
                raise AutoRoutingServiceError("adapt rollback requires a revision")
            if not control.frozen:
                raise AutoRoutingServiceError(
                    "adapt rollback requires the profile to be frozen"
                )
            revision = self.store.read_profile_revision(revision_id)
            if (
                revision is None
                or not revision.complete
                or revision.authority_id != authority_id
                or revision.profile_id != profile_id
            ):
                raise AutoRoutingServiceError(
                    "rollback revision must be a complete revision for this authority/profile"
                )
            if revision.revision_id == control.active_revision_id:
                raise AutoRoutingServiceError(
                    "rollback revision must differ from the active revision"
                )
            validate_overlay(config, revision.overlay)
            requested = {
                "revision_id": revision.revision_id,
                "revision_checksum": self._profile_revision_checksum(revision),
            }
        else:
            raise AutoRoutingServiceError(
                f"unsupported adaptation control action: {action}"
            )
        precondition = {
            "authority_id": authority_id,
            "profile_id": profile_id,
            "active_revision_id": control.active_revision_id,
            "control_revision_id": control.control_revision_id,
            "challenger_revision_id": control.challenger_revision_id,
            "experiment_phase": control.experiment_phase,
            "generation": control.generation,
            "frozen": control.frozen,
            "cooldown_until": control.cooldown_until,
            "action": action,
            "requested": requested,
        }
        return precondition, control

    def preview_adaptation_control(
        self,
        *,
        action: str,
        profile_id: str,
        revision_id: str | None = None,
    ) -> dict[str, Any]:
        """Build an argument-bound preview for one guarded adaptive mutation."""
        precondition, _control = self._adaptation_control_precondition(
            action=action,
            profile_id=profile_id,
            revision_id=revision_id,
        )
        return {
            "apply": False,
            "action": action,
            "profile_id": profile_id,
            "precondition": precondition,
            "precondition_hash": _checksum(_canonical_json(precondition)),
        }

    def apply_adaptation_control(
        self,
        *,
        action: str,
        profile_id: str,
        expected_hash: str,
        revision_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply one exact preview using the profile generation as the CAS."""
        if not isinstance(expected_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_hash
        ):
            raise AutoRoutingServiceError(
                "adaptation apply requires a SHA-256 precondition hash"
            )
        with profile_config_lock(self.config_path):
            preview = self.preview_adaptation_control(
                action=action,
                profile_id=profile_id,
                revision_id=revision_id,
            )
            if not hmac.compare_digest(preview["precondition_hash"], expected_hash):
                raise AutoRoutingServiceError(
                    "adaptation precondition changed; run the preview again"
                )
            precondition = preview["precondition"]
            authority_id = precondition["authority_id"]
            generation = precondition["generation"]
            previous_active_revision_id = precondition["active_revision_id"]
            if action in {"freeze", "unfreeze"}:
                self.store.set_profile_freeze(
                    authority_id,
                    profile_id,
                    frozen=bool(precondition["requested"]["frozen"]),
                    expected_generation=generation,
                )
            elif action == "rollback":
                self.store.rollback_profile_revision(
                    authority_id=authority_id,
                    profile_id=profile_id,
                    revision_id=revision_id or "",
                    expected_target_checksum=precondition["requested"][
                        "revision_checksum"
                    ],
                    expected_generation=generation,
                )
            else:  # pragma: no cover - preview validates the closed action set
                raise AutoRoutingServiceError(
                    f"unsupported adaptation control action: {action}"
                )
            return {
                **self.adaptation_status(profile_id),
                "apply": True,
                "action": action,
                "previous_active_revision_id": previous_active_revision_id,
                "applied_precondition_hash": expected_hash,
            }

    def close(self) -> None:
        """Release the profile-local store owned by this service."""
        self.store.close()

    def _runtime_root_config(self) -> dict[str, Any]:
        try:
            root = fast_safe_load(self.config_path.read_bytes())
        except Exception as error:
            raise AutoRoutingServiceError("config could not be read") from error
        if not isinstance(root, Mapping):
            raise AutoRoutingServiceError("config root must be a mapping")
        return dict(root)

    @staticmethod
    def _runtime_fact_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "complexity",
            "declared_child_tools",
            "domains",
            "required_capabilities",
            "required_modalities",
            "risk_class",
        }
        return {key: value for key, value in metadata.items() if key in allowed}

    @staticmethod
    def _runtime_timestamp() -> str:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _runtime_revision(value: Any) -> str:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json", warnings=False)
        return _checksum(_canonical_json(value))

    @staticmethod
    def static_profile_revision_id(authority_id: str, profile_id: str) -> str:
        """Return the immutable static marker for one authority/profile pair."""
        return static_adaptive_revision_id(authority_id, profile_id)

    def resolve_effective_adaptation_snapshot(
        self,
        *,
        authority_id: str,
        profile_ids: tuple[str, ...],
    ) -> dict[str, str]:
        """Read one complete profile-wide pointer snapshot or fail closed."""
        if len(profile_ids) != len(set(profile_ids)):
            raise AutoRoutingServiceError("adaptive snapshot profiles must be unique")
        snapshot: dict[str, str] = {}
        revisions = self.store.read_active_profile_revision_snapshot(
            authority_id,
            profile_ids,
        )
        for profile_id, (revision, _generation) in revisions.items():
            if revision is None:
                snapshot[profile_id] = self.static_profile_revision_id(
                    authority_id,
                    profile_id,
                )
                continue
            if (
                not revision.complete
                or revision.authority_id != authority_id
                or revision.profile_id != profile_id
            ):
                raise AutoRoutingServiceError(
                    "active profile adaptive revision is invalid"
                )
            snapshot[profile_id] = revision.revision_id
        return snapshot

    def _profiles_for_adaptation_snapshot(
        self,
        config: AutoRoutingConfig,
        snapshot: Mapping[str, str],
        inventory: Any,
    ) -> tuple[RouteProfile, ...]:
        """Materialize exactly the complete recorded profile snapshot."""
        if set(snapshot) != set(config.profiles):
            raise AutoRoutingServiceError(
                "adaptive snapshot must attest every candidate profile"
            )
        overlays: dict[str, AdaptiveOverlay] = {}
        authority_id = authority_revision(config)
        for profile_id, revision_id in snapshot.items():
            static_id = self.static_profile_revision_id(authority_id, profile_id)
            if revision_id == static_id:
                continue
            revision = self.store.read_profile_revision(revision_id)
            if (
                revision is None
                or not revision.complete
                or revision.authority_id != authority_id
                or revision.profile_id != profile_id
            ):
                raise AutoRoutingServiceError(
                    "adaptive snapshot references an invalid profile revision"
                )
            validate_overlay(config, revision.overlay)
            overlays[profile_id] = revision.overlay
        effective = materialize_profiles(config, overlays)
        return self._runtime_current_profiles(effective, inventory)

    def _provisional_canary_assignment(
        self,
        *,
        config: AutoRoutingConfig,
        request: AgentRuntimeRequest,
        assessment: Any,
        inventory: Any,
        selected_profile_id: str | None,
        context_bucket_id: str,
    ) -> AdaptiveCanaryAssignment | None:
        """Calculate or recover an exact arm without creating durable state."""
        if selected_profile_id is None:
            return None
        profile = config.profiles.get(selected_profile_id)
        if profile is None:
            return None
        authority_id = authority_revision(config)
        control = self.store.read_profile_control(authority_id, selected_profile_id)
        if (
            control.experiment_phase != "canary"
            or control.control_revision_id is None
            or control.challenger_revision_id is None
        ):
            return None
        verified = {
            runtime.key.stable_id()
            for runtime in inventory.runtimes
            if runtime.state == "verified"
        }
        challenger = self.store.read_profile_revision(
            control.challenger_revision_id
        )
        if (
            challenger is None
            or challenger.authority_id != authority_id
            or challenger.profile_id != selected_profile_id
            or challenger.parent_revision_id != control.control_revision_id
            or challenger.explanation.control_revision_id
            != control.control_revision_id
            or challenger.explanation.context_bucket_id != context_bucket_id
        ):
            return None
        challenger_available = (
            challenger.overlay.ordered_primary_runtime_ids[0] in verified
        )
        limits = profile.limits
        canary_high_risk = bool(
            limits is not None and limits.canary_high_risk_tasks is True
        )
        metadata = request.context.metadata
        if not canary_eligible(
            scope=request.context.scope,
            is_resume=request.context.is_resume,
            is_compression=bool(metadata.get("is_compression")),
            manual_override=request.context.manual_runtime_pin,
            fixed_runtime=bool(
                metadata.get("fixed_delegation_provider")
                or metadata.get("fixed_delegation_model")
            ),
            risk_class=assessment.risk_class,
            canary_high_risk_tasks=canary_high_risk,
            policy_compliant=True,
            frozen=control.frozen,
            adaptation_enabled=profile.adaptation.enabled,
            challenger_available=challenger_available,
            canary_fraction=profile.adaptation.canary_fraction,
        ):
            return None
        operation_hash = operation_identity_hash(
            scope=request.context.scope,
            session_id=request.context.session_id,
            task_id=request.context.task_id,
            operation_id=request.context.operation_id,
            task_index=request.context.task_index,
        )
        existing = self.store.read_canary_assignment(
            authority_id,
            selected_profile_id,
            operation_hash,
        )
        if existing is not None:
            if (
                existing.control_revision_id != control.control_revision_id
                or existing.challenger_revision_id
                != control.challenger_revision_id
            ):
                raise AutoRoutingServiceError(
                    "recorded canary assignment no longer matches its experiment"
                )
            return existing
        key = ensure_profile_canary_key(
            self.hermes_home,
            config_path=self.config_path,
        )
        arm = deterministic_canary_arm(
            key,
            selected_profile_id,
            operation_hash,
            profile.adaptation.canary_fraction,
        )
        seed = _canonical_json({
            "authority_id": authority_id,
            "profile_id": selected_profile_id,
            "operation_identity_hash": operation_hash,
            "control_revision_id": control.control_revision_id,
            "challenger_revision_id": control.challenger_revision_id,
            "arm": arm,
        })
        return AdaptiveCanaryAssignment(
            assignment_id=_checksum(seed),
            authority_id=authority_id,
            profile_id=selected_profile_id,
            operation_identity_hash=operation_hash,
            context_bucket_id=context_bucket_id,
            control_revision_id=control.control_revision_id,
            challenger_revision_id=control.challenger_revision_id,
            arm=arm,
            created_at=self._runtime_timestamp(),
        )

    def _persist_matching_canary_assignment(
        self,
        assignment: AdaptiveCanaryAssignment,
        *,
        selected_profile_id: str | None,
        selected_revision_id: str | None,
        selected_runtime_id: str,
        selected_reasoning_effort: str | None = None,
    ) -> AdaptiveCanaryAssignment | None:
        """Persist only an arm that exactly produced the final selected route."""
        if not self._canary_assignment_matches_final_route(
            assignment,
            selected_profile_id=selected_profile_id,
            selected_revision_id=selected_revision_id,
            selected_runtime_id=selected_runtime_id,
            selected_reasoning_effort=selected_reasoning_effort,
        ):
            return None
        try:
            return self.store.get_or_create_canary_assignment(assignment)
        except (InvalidLifecycleTransition, ProfileFrozen, ProfileStateConflict):
            # The route was selected against a provisional snapshot. A concurrent
            # freeze or lifecycle transition must leave no durable assignment.
            return None

    def _canary_assignment_matches_final_route(
        self,
        assignment: AdaptiveCanaryAssignment,
        *,
        selected_profile_id: str | None,
        selected_revision_id: str | None,
        selected_runtime_id: str,
        selected_reasoning_effort: str | None,
    ) -> bool:
        """Require an arm to attest the exact runtime and effort that will run."""
        expected_revision = (
            assignment.challenger_revision_id
            if assignment.arm == "challenger"
            else assignment.control_revision_id
        )
        if (
            selected_profile_id != assignment.profile_id
            or selected_revision_id != expected_revision
        ):
            return False
        revision = self.store.read_profile_revision(expected_revision)
        if revision is None:
            return False
        expected_runtime = revision.overlay.ordered_primary_runtime_ids[0]
        expected_effort = revision.overlay.reasoning_defaults.get(expected_runtime)
        return bool(
            expected_effort is not None
            and selected_runtime_id == expected_runtime
            and selected_reasoning_effort == expected_effort
        )

    def _proposal_context_events(
        self,
        authority_id: str,
        profile_id: str,
    ) -> tuple[EvidenceEvent, ...]:
        """Return only initial context evidence from the current authority."""
        events: list[EvidenceEvent] = []
        for event in self.store.list_evidence_events(profile_id=profile_id):
            if not event.is_initial_routing_task or event.context_bucket is None:
                continue
            decision = self.store.read_decision(event.decision_id)
            if (
                decision is None
                or decision.authority_revision != authority_id
                or decision.selected_profile_id != profile_id
            ):
                continue
            events.append(event)
        return tuple(
            sorted(events, key=lambda item: (item.observed_at, item.evidence_id))
        )

    def list_adaptation_observations(
        self,
        *,
        authority_id: str,
        profile_id: str,
        context_bucket_id: str,
        runtime_id: str,
        reasoning_effort: str,
        assignment_id: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Return validated, content-free quality observations for one exact arm."""
        events = self.store.list_evidence_events(
            profile_id=profile_id,
            runtime_id=runtime_id,
            reasoning_effort=reasoning_effort,
        )
        observations: list[dict[str, Any]] = []
        for event in events:
            bucket = getattr(event, "context_bucket", None)
            if (
                not getattr(event, "is_initial_routing_task", False)
                or event.profile_id != profile_id
                or event.runtime_id != runtime_id
                or event.reasoning_effort != reasoning_effort
                or bucket is None
                or bucket.bucket_id != context_bucket_id
            ):
                continue
            decision = self.store.read_decision(event.decision_id)
            if (
                decision is None
                or decision.authority_revision != authority_id
                or decision.selected_profile_id != profile_id
            ):
                continue
            recorded_assignment = decision.adaptive_assignment_id
            if assignment_id is not None and recorded_assignment != assignment_id:
                continue
            observations.append({
                "evidence_id": event.evidence_id,
                "parent_evidence_id": event.parent_evidence_id,
                "decision_id": event.decision_id,
                "assignment_id": recorded_assignment,
                "profile_adaptive_revision_id": (
                    decision.profile_adaptive_revision_id
                ),
                "is_initial_routing_task": True,
                "source": event.source,
                "outcome": event.outcome,
                "feedback_value": event.feedback_value,
                "retry_count": getattr(event, "retry_count", 0),
                "cost_usd": getattr(event, "cost_usd", 0.0),
                "latency_seconds": getattr(event, "latency_seconds", None),
                "observed_at": event.observed_at,
            })
        return tuple(
            sorted(
                observations,
                key=lambda item: (str(item["observed_at"]), item["evidence_id"]),
            )
        )

    @staticmethod
    def _operational_guardrail_reason(
        *,
        config: AutoRoutingConfig,
        profile: RouteProfile,
        runtime_id: str,
        observations: tuple[Mapping[str, Any], ...],
        now: datetime | None = None,
        experiment_spend_usd: float | None = None,
    ) -> str | None:
        """Evaluate operational limits separately from quality outcomes."""
        target = next(
            (
                candidate
                for candidate in profile.primary_choices()
                if candidate.runtime.stable_id() == runtime_id
            ),
            None,
        )
        if target is None:
            return "policy_guardrail"
        if (
            target.runtime.provider in config.policy.denied_providers
            or target.runtime.model in config.policy.denied_models
        ):
            return "policy_guardrail"
        if any(
            item.get("source") == "hermes_turn_outcome"
            and item.get("outcome") == "failed"
            for item in observations
        ):
            # A persisted provider/turn failure is an operational stop signal,
            # not a fractional quality sample that successes can dilute.
            return "failure_guardrail"
        if any(int(item.get("retry_count") or 0) >= 3 for item in observations):
            return "retry_guardrail"

        cost_limits = [float(config.policy.max_estimated_task_cost_usd)]
        latency_limits = [float(config.policy.max_estimated_latency_seconds)]
        if profile.limits is not None:
            if profile.limits.max_estimated_task_cost_usd is not None:
                cost_limits.append(float(profile.limits.max_estimated_task_cost_usd))
            if profile.limits.max_estimated_latency_seconds is not None:
                latency_limits.append(
                    float(profile.limits.max_estimated_latency_seconds)
                )
        if target.max_estimated_task_cost_usd is not None:
            cost_limits.append(float(target.max_estimated_task_cost_usd))
        if target.max_estimated_latency_seconds is not None:
            latency_limits.append(float(target.max_estimated_latency_seconds))

        costs = tuple(float(item.get("cost_usd") or 0.0) for item in observations)
        if any(value > min(cost_limits) for value in costs):
            return "cost_guardrail"
        latencies = tuple(
            float(item["latency_seconds"])
            for item in observations
            if item.get("latency_seconds") is not None
        )
        if any(value > min(latency_limits) for value in latencies):
            return "latency_guardrail"
        budget_costs = costs
        if now is not None:
            budget_day = now.astimezone(UTC).date()
            budget_costs = tuple(
                float(item.get("cost_usd") or 0.0)
                for item in observations
                if datetime.fromisoformat(
                    str(item["observed_at"]).replace("Z", "+00:00")
                ).astimezone(UTC).date()
                == budget_day
            )
        daily_experiment_cost = (
            sum(budget_costs)
            if experiment_spend_usd is None
            else float(experiment_spend_usd)
        )
        if daily_experiment_cost > float(
            config.policy.max_experiment_cost_usd_per_day
        ):
            return "budget_guardrail"
        return None

    def _daily_experiment_spend_usd(
        self,
        authority_id: str,
        moment: datetime,
    ) -> float:
        """Sum persisted canary-assigned turn cost for the current UTC day."""
        utc_moment = moment.astimezone(UTC)
        day_start = utc_moment.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        ).isoformat().replace("+00:00", "Z")
        total = 0.0
        for event in self.store.list_evidence_events(
            observed_at_or_after=day_start
        ):
            if not event.is_initial_routing_task or event.source != "hermes_turn_outcome":
                continue
            decision = self.store.read_decision(event.decision_id)
            if (
                decision is None
                or decision.authority_revision != authority_id
                or decision.adaptive_assignment_id is None
            ):
                continue
            total += float(event.cost_usd)
        return total

    def maybe_advance_adaptation(
        self,
        *,
        profile_id: str,
        now: str | datetime | None = None,
    ) -> AdaptationAdvance:
        """Run at most one leased, profile-local lifecycle mutation."""
        config = self._configured_authority()
        profile = config.profiles.get(profile_id)
        if profile is None:
            return AdaptationAdvance("hold", "profile_missing")
        if not profile.adaptation.enabled:
            return AdaptationAdvance("disabled", "profile_opt_out")
        authority_id = authority_revision(config)
        control = self.store.read_profile_control(authority_id, profile_id)
        if control.frozen:
            return AdaptationAdvance("frozen", "profile_frozen")
        moment = (
            datetime.now(UTC)
            if now is None
            else _parse_timestamp(now)
            if isinstance(now, str)
            else now.astimezone(UTC)
        )
        if (
            control.experiment_phase == "cooldown"
            and control.cooldown_until is not None
            and _parse_timestamp(control.cooldown_until) > moment
        ):
            return AdaptationAdvance(
                "cooldown",
                "cooldown_active",
                retry_after_seconds=max(
                    0.0,
                    (_parse_timestamp(control.cooldown_until) - moment).total_seconds(),
                ),
            )
        owner_id = f"optimizer-{uuid.uuid4().hex}"
        lease = self.store.acquire_optimizer_lease(
            authority_id,
            profile_id,
            owner_id,
            moment,
            10.0,
        )
        if lease is None:
            return AdaptationAdvance("hold", "lease_conflict", retry_after_seconds=1.0)
        try:
            return self._advance_adaptation_with_lease(
                config=config,
                profile=profile,
                authority_id=authority_id,
                moment=moment,
            )
        except Exception as error:
            # Optimizer contention and stale generations are expected holds at the
            # runtime boundary; immutable storage continues to fail closed.
            return AdaptationAdvance("hold", type(error).__name__)
        finally:
            self.store.release_optimizer_lease(lease)

    def _advance_adaptation_with_lease(
        self,
        *,
        config: AutoRoutingConfig,
        profile: RouteProfile,
        authority_id: str,
        moment: datetime,
    ) -> AdaptationAdvance:
        """Advance one already-leased experiment without external I/O."""
        control = self.store.read_profile_control(authority_id, profile.profile_id)
        now_text = moment.isoformat().replace("+00:00", "Z")
        if control.experiment_phase in {"cooldown", "rolled_back"}:
            event = self._adaptation_event(
                authority_id,
                profile.profile_id,
                control.active_revision_id,
                "eligible",
                "cooldown_complete",
                now_text,
            )
            updated = self.store.transition_profile_experiment(
                authority_id,
                profile.profile_id,
                active_revision_id=control.active_revision_id,
                control_revision_id=None,
                challenger_revision_id=None,
                experiment_phase="eligible",
                cooldown_until=None,
                rejection_count=control.rejection_count,
                expected_generation=control.generation,
                event=event,
            )
            return AdaptationAdvance("eligible", "cooldown_complete", updated.active_revision_id)

        if control.experiment_phase == "eligible":
            context_events = self._proposal_context_events(
                authority_id,
                profile.profile_id,
            )
            if not context_events:
                return AdaptationAdvance("hold", "no_local_context")
            context_bucket_id = max(
                context_events,
                key=lambda item: (item.observed_at, item.evidence_id),
            ).context_bucket.bucket_id
            context_events = tuple(
                event
                for event in context_events
                if event.context_bucket.bucket_id == context_bucket_id
            )
            control_revision, generation = self.store.read_active_profile_revision(
                authority_id,
                profile.profile_id,
            )
            if control_revision is None:
                control_revision = self._static_profile_revision(
                    authority_id,
                    profile,
                    now_text,
                )
                generation = self.store.publish_profile_revision(
                    control_revision,
                    expected_revision_id=None,
                    expected_generation=generation,
                )
            challenger = self._challenger_profile_revision(
                config,
                authority_id,
                profile,
                control_revision,
                context_bucket_id,
                tuple(event.evidence_id for event in context_events),
                now_text,
            )
            generation = self.store.insert_inactive_profile_revision(
                challenger,
                expected_active_revision_id=control_revision.revision_id,
                expected_generation=generation,
            )
            validated = self.store.transition_profile_experiment(
                authority_id,
                profile.profile_id,
                active_revision_id=control_revision.revision_id,
                control_revision_id=control_revision.revision_id,
                challenger_revision_id=challenger.revision_id,
                experiment_phase="validated",
                cooldown_until=None,
                rejection_count=control.rejection_count,
                expected_generation=generation,
                event=self._adaptation_event(
                    authority_id,
                    profile.profile_id,
                    challenger.revision_id,
                    "validated",
                    "authority_overlay_validated",
                    now_text,
                ),
            )
            canary = self.store.transition_profile_experiment(
                authority_id,
                profile.profile_id,
                active_revision_id=control_revision.revision_id,
                control_revision_id=control_revision.revision_id,
                challenger_revision_id=challenger.revision_id,
                experiment_phase="canary",
                cooldown_until=None,
                rejection_count=control.rejection_count,
                expected_generation=validated.generation,
                event=self._adaptation_event(
                    authority_id,
                    profile.profile_id,
                    challenger.revision_id,
                    "canary",
                    "canary_started",
                    now_text,
                ),
            )
            return AdaptationAdvance("canary", "canary_started", canary.challenger_revision_id)

        if control.experiment_phase == "promoted":
            return self._evaluate_promoted(
                config=config,
                profile=profile,
                control=control,
                authority_id=authority_id,
                moment=moment,
            )
        if control.experiment_phase != "canary":
            return AdaptationAdvance("hold", f"phase_{control.experiment_phase}")
        return self._evaluate_canary(
            config=config,
            profile=profile,
            control=control,
            authority_id=authority_id,
            moment=moment,
        )

    @staticmethod
    def _adaptation_event(
        authority_id: str,
        profile_id: str,
        revision_id: str | None,
        event_type: str,
        reason_code: str,
        created_at: str,
    ) -> AdaptiveLifecycleEvent:
        seed = _canonical_json({
            "authority_id": authority_id,
            "profile_id": profile_id,
            "revision_id": revision_id,
            "event_type": event_type,
            "reason_code": reason_code,
            "created_at": created_at,
        })
        return AdaptiveLifecycleEvent(
            event_id=_checksum(seed),
            authority_id=authority_id,
            profile_id=profile_id,
            revision_id=revision_id,
            event_type=event_type,
            reason_code=reason_code,
            created_at=created_at,
        )

    @staticmethod
    def _profile_overlay(profile: RouteProfile) -> AdaptiveOverlay:
        return AdaptiveOverlay(
            profile_id=profile.profile_id,
            ordered_primary_runtime_ids=tuple(
                target.runtime.stable_id() for target in profile.primary_choices()
            ),
            reasoning_defaults={
                target.runtime.stable_id(): target.reasoning.default
                for target in profile.primary_choices()
            },
        )

    def _static_profile_revision(
        self,
        authority_id: str,
        profile: RouteProfile,
        created_at: str,
    ) -> AdaptiveProfileRevision:
        overlay = self._profile_overlay(profile)
        return AdaptiveProfileRevision(
            revision_id=self.static_profile_revision_id(authority_id, profile.profile_id),
            authority_id=authority_id,
            profile_id=profile.profile_id,
            overlay=overlay,
            explanation=AdaptiveExplanation(reason_codes=("static_authority",)),
            lifecycle="eligible",
            created_at=created_at,
        )

    def _challenger_profile_revision(
        self,
        config: AutoRoutingConfig,
        authority_id: str,
        profile: RouteProfile,
        parent: AdaptiveProfileRevision,
        context_bucket_id: str,
        evidence_ids: tuple[str, ...],
        created_at: str,
    ) -> AdaptiveProfileRevision:
        choices = profile.primary_choices()
        challengers = choices[1:]
        if not challengers:  # pragma: no cover - authority validation prevents this
            raise AutoRoutingServiceError("adaptive profile has no approved challenger")
        candidate_ids = tuple(target.runtime.stable_id() for target in challengers)
        prior = tuple(
            revision
            for revision in self.store.list_profile_revisions(
                authority_id,
                profile.profile_id,
            )
            if revision.parent_revision_id == parent.revision_id
            and revision.explanation.control_revision_id == parent.revision_id
            and revision.explanation.context_bucket_id == context_bucket_id
            and (
                revision.explanation.challenger_runtime_id
                or revision.overlay.ordered_primary_runtime_ids[0]
            )
            in candidate_ids
        )
        tried_ids = {
            revision.explanation.challenger_runtime_id
            or revision.overlay.ordered_primary_runtime_ids[0]
            for revision in prior
        }
        untried = tuple(
            (index, target)
            for index, target in enumerate(challengers)
            if target.runtime.stable_id() not in tried_ids
        )
        if untried:
            challenger_index, challenger = untried[0]
            selection_mode = "untried"
        else:
            challenger_index = len(prior) % len(challengers)
            challenger = challengers[challenger_index]
            selection_mode = "cycle"
        challenger_runtime_id = challenger.runtime.stable_id()
        ordered = (
            challenger,
            *(
                target
                for target in choices
                if target.runtime.stable_id() != challenger_runtime_id
            ),
        )
        overlay = AdaptiveOverlay(
            profile_id=profile.profile_id,
            ordered_primary_runtime_ids=tuple(
                target.runtime.stable_id() for target in ordered
            ),
            reasoning_defaults={
                target.runtime.stable_id(): target.reasoning.default
                for target in ordered
            },
        )
        validate_overlay(config=config, overlay=overlay)
        seed = _canonical_json({
            "authority_id": authority_id,
            "profile_id": profile.profile_id,
            "parent_revision_id": parent.revision_id,
            "overlay": overlay.model_dump(mode="json"),
            "context_bucket_id": context_bucket_id,
            "evidence_ids": sorted(set(evidence_ids)),
            "challenger_runtime_id": challenger_runtime_id,
            "prior_challenger_trials": len(prior),
        })
        return AdaptiveProfileRevision(
            revision_id=_checksum(seed),
            authority_id=authority_id,
            profile_id=profile.profile_id,
            parent_revision_id=parent.revision_id,
            overlay=overlay,
            explanation=AdaptiveExplanation(
                reason_codes=("approved_primary_challenger",),
                evidence_ids=tuple(sorted(set(evidence_ids))),
                context_bucket_id=context_bucket_id,
                control_revision_id=parent.revision_id,
                challenger_runtime_id=challenger_runtime_id,
                counts={
                    "challenger_primary_index": challenger_index,
                    "prior_challenger_trials": len(prior),
                },
                labels={"challenger_selection": selection_mode},
            ),
            lifecycle="validated",
            created_at=created_at,
        )

    def _evaluate_canary(
        self,
        *,
        config: AutoRoutingConfig,
        profile: RouteProfile,
        control: Any,
        authority_id: str,
        moment: datetime,
    ) -> AdaptationAdvance:
        control_revision = self.store.read_profile_revision(
            control.control_revision_id
        )
        challenger_revision = self.store.read_profile_revision(
            control.challenger_revision_id
        )
        if control_revision is None or challenger_revision is None:
            raise AutoRoutingServiceError("canary revision pair is unavailable")
        validate_overlay(config, control_revision.overlay)
        validate_overlay(config, challenger_revision.overlay)
        context_bucket_id = challenger_revision.explanation.context_bucket_id
        if context_bucket_id is None:
            raise AutoRoutingServiceError("canary context is unavailable")
        targets = {
            target.runtime.stable_id(): target for target in profile.primary_choices()
        }
        control_runtime_id = control_revision.overlay.ordered_primary_runtime_ids[0]
        challenger_runtime_id = (
            challenger_revision.overlay.ordered_primary_runtime_ids[0]
        )
        control_observations = self.list_adaptation_observations(
            authority_id=authority_id,
            profile_id=profile.profile_id,
            context_bucket_id=context_bucket_id,
            runtime_id=control_runtime_id,
            reasoning_effort=control_revision.overlay.reasoning_defaults.get(
                control_runtime_id,
                targets[control_runtime_id].reasoning.default,
            ),
        )
        challenger_observations = self.list_adaptation_observations(
            authority_id=authority_id,
            profile_id=profile.profile_id,
            context_bucket_id=context_bucket_id,
            runtime_id=challenger_runtime_id,
            reasoning_effort=challenger_revision.overlay.reasoning_defaults.get(
                challenger_runtime_id,
                targets[challenger_runtime_id].reasoning.default,
            ),
        )
        control_observations = tuple(
            item
            for item in control_observations
            if item["profile_adaptive_revision_id"] == control_revision.revision_id
            and item["assignment_id"] is not None
        )
        challenger_observations = tuple(
            item
            for item in challenger_observations
            if item["profile_adaptive_revision_id"]
            == challenger_revision.revision_id
            and item["assignment_id"] is not None
        )
        adverse = next(
            (
                item
                for item in challenger_observations
                if item["source"] == "user_feedback"
                and item["feedback_value"] in {"rejected", "corrected"}
            ),
            None,
        )
        guardrail_reason = self._operational_guardrail_reason(
            config=config,
            profile=profile,
            runtime_id=challenger_runtime_id,
            observations=challenger_observations,
            now=moment,
            experiment_spend_usd=self._daily_experiment_spend_usd(
                authority_id,
                moment,
            ),
        )
        if adverse is None and guardrail_reason is not None:
            adverse = {"feedback_value": guardrail_reason}
        control_quality = summarize_quality(control_observations)
        challenger_quality = summarize_quality(challenger_observations)
        decision = promotion_decision(
            control_quality,
            challenger_quality,
            minimum_samples=profile.adaptation.minimum_comparable_samples,
            confidence_level=profile.adaptation.confidence_level,
        )
        if adverse is None:
            regression = rollback_decision(
                (*control_observations, *challenger_observations),
                assignment_id=str(
                    challenger_observations[0]["assignment_id"]
                    if challenger_observations
                    else "unassigned"
                ),
                threshold=profile.adaptation.observed_regression_threshold,
                control=control_quality,
                challenger=challenger_quality,
                minimum_samples=profile.adaptation.minimum_comparable_samples,
            )
            if regression.action in {"reject", "rollback"}:
                adverse = {"feedback_value": regression.reason}
        now_text = moment.isoformat().replace("+00:00", "Z")
        if adverse is not None:
            rejection_reason = (
                guardrail_reason
                if guardrail_reason is not None
                else "canary_quality_rejected"
            )
            rejected = self.store.transition_profile_experiment(
                authority_id,
                profile.profile_id,
                active_revision_id=control_revision.revision_id,
                control_revision_id=control_revision.revision_id,
                challenger_revision_id=challenger_revision.revision_id,
                experiment_phase="rejected",
                cooldown_until=None,
                rejection_count=control.rejection_count + 1,
                expected_generation=control.generation,
                event=self._adaptation_event(
                    authority_id,
                    profile.profile_id,
                    challenger_revision.revision_id,
                    "rejected",
                    rejection_reason,
                    now_text,
                ),
            )
            cooldown_seconds = min(
                profile.adaptation.cooldown_base_seconds
                * 2 ** max(0, rejected.rejection_count - 1),
                profile.adaptation.cooldown_max_seconds,
            )
            cooldown_until = (
                moment + timedelta(seconds=cooldown_seconds)
            ).isoformat().replace("+00:00", "Z")
            cooled = self.store.transition_profile_experiment(
                authority_id,
                profile.profile_id,
                active_revision_id=control_revision.revision_id,
                control_revision_id=control_revision.revision_id,
                challenger_revision_id=challenger_revision.revision_id,
                experiment_phase="cooldown",
                cooldown_until=cooldown_until,
                rejection_count=rejected.rejection_count,
                expected_generation=rejected.generation,
                event=self._adaptation_event(
                    authority_id,
                    profile.profile_id,
                    control_revision.revision_id,
                    "cooldown",
                    "rejection_cooldown",
                    now_text,
                ),
            )
            return AdaptationAdvance("rejected", rejection_reason, cooled.active_revision_id)
        if decision.action != "promote":
            return AdaptationAdvance("hold", decision.reason)
        promoted = self.store.transition_profile_experiment(
            authority_id,
            profile.profile_id,
            active_revision_id=challenger_revision.revision_id,
            control_revision_id=control_revision.revision_id,
            challenger_revision_id=challenger_revision.revision_id,
            experiment_phase="promoted",
            cooldown_until=None,
            rejection_count=control.rejection_count,
            expected_generation=control.generation,
            event=self._adaptation_event(
                authority_id,
                profile.profile_id,
                challenger_revision.revision_id,
                "promoted",
                "posterior_separated",
                now_text,
            ),
        )
        return AdaptationAdvance("promoted", decision.reason, promoted.active_revision_id)

    def _evaluate_promoted(
        self,
        *,
        config: AutoRoutingConfig,
        profile: RouteProfile,
        control: Any,
        authority_id: str,
        moment: datetime,
    ) -> AdaptationAdvance:
        """Roll back an exact promoted revision on explicit or sampled regression."""
        control_revision = self.store.read_profile_revision(
            control.control_revision_id
        )
        challenger_revision = self.store.read_profile_revision(
            control.challenger_revision_id
        )
        if control_revision is None or challenger_revision is None:
            raise AutoRoutingServiceError("promoted revision pair is unavailable")
        validate_overlay(config, control_revision.overlay)
        validate_overlay(config, challenger_revision.overlay)
        explanation = challenger_revision.explanation
        context_bucket_id = (
            explanation.get("context_bucket_id")
            if isinstance(explanation, Mapping)
            else explanation.context_bucket_id
        )
        if context_bucket_id is None:
            raise AutoRoutingServiceError("promoted context is unavailable")
        targets = {
            target.runtime.stable_id(): target for target in profile.primary_choices()
        }

        def observations_for(revision: AdaptiveProfileRevision) -> tuple[dict[str, Any], ...]:
            runtime_id = revision.overlay.ordered_primary_runtime_ids[0]
            values = self.list_adaptation_observations(
                authority_id=authority_id,
                profile_id=profile.profile_id,
                context_bucket_id=context_bucket_id,
                runtime_id=runtime_id,
                reasoning_effort=revision.overlay.reasoning_defaults.get(
                    runtime_id,
                    targets[runtime_id].reasoning.default,
                ),
            )
            return tuple(
                item
                for item in values
                if item["profile_adaptive_revision_id"] == revision.revision_id
            )

        control_observations = observations_for(control_revision)
        challenger_observations = observations_for(challenger_revision)
        challenger_runtime_id = (
            challenger_revision.overlay.ordered_primary_runtime_ids[0]
        )
        guardrail_reason = self._operational_guardrail_reason(
            config=config,
            profile=profile,
            runtime_id=challenger_runtime_id,
            observations=challenger_observations,
            now=moment,
            experiment_spend_usd=self._daily_experiment_spend_usd(
                authority_id,
                moment,
            ),
        )
        adverse = any(
            item["source"] == "user_feedback"
            and item["feedback_value"] in {"rejected", "corrected"}
            for item in challenger_observations
        )
        regression = rollback_decision(
            challenger_observations,
            assignment_id="promoted-unassigned",
            threshold=profile.adaptation.observed_regression_threshold,
            control=summarize_quality(control_observations),
            challenger=summarize_quality(challenger_observations),
            minimum_samples=profile.adaptation.minimum_comparable_samples,
        )
        if (
            guardrail_reason is None
            and not adverse
            and regression.action != "rollback"
        ):
            return AdaptationAdvance("hold", regression.reason)
        rejection_count = control.rejection_count + 1
        cooldown_seconds = min(
            profile.adaptation.cooldown_base_seconds
            * 2 ** max(0, rejection_count - 1),
            profile.adaptation.cooldown_max_seconds,
        )
        now_text = moment.isoformat().replace("+00:00", "Z")
        cooldown_until = (
            moment + timedelta(seconds=cooldown_seconds)
        ).isoformat().replace("+00:00", "Z")
        rolled_back = self.store.transition_profile_experiment(
            authority_id,
            profile.profile_id,
            active_revision_id=control_revision.revision_id,
            control_revision_id=control_revision.revision_id,
            challenger_revision_id=challenger_revision.revision_id,
            experiment_phase="cooldown",
            cooldown_until=cooldown_until,
            rejection_count=rejection_count,
            expected_generation=control.generation,
            event=self._adaptation_event(
                authority_id,
                profile.profile_id,
                control_revision.revision_id,
                "cooldown",
                guardrail_reason or "promoted_regression_rollback",
                now_text,
            ),
        )
        return AdaptationAdvance(
            "rollback",
            guardrail_reason or "promoted_regression_rollback",
            rolled_back.active_revision_id,
        )

    @staticmethod
    def _inherited_safe_default_reasoning(
        reasoning_support: Any,
        requested_effort: str | None,
    ) -> tuple[str, tuple[str, ...]]:
        """Apply one deterministic inherited-baseline effort policy."""
        supported = tuple(
            effort
            for effort in REASONING_EFFORT_ORDER
            if effort in reasoning_support.efforts
        )
        if not supported:
            supported = ("none",)
        default = requested_effort if requested_effort in supported else supported[0]
        return default, supported

    def _runtime_safe_default_target(
        self,
        config: AutoRoutingConfig,
        inventory: Any,
        request: AgentRuntimeRequest,
    ) -> RoutingTarget:
        if isinstance(config.safe_default, RoutingTarget):
            return self._runtime_current_target(config.safe_default, inventory)
        resolved = self.adapter.resolve_inherited_baseline(inventory.revision)
        if resolved is None:
            raise AutoRoutingServiceError(
                "inherited safe default is not an exact executable runtime"
            )
        runtime_id = resolved.runtime_key.stable_id()
        matches = tuple(
            runtime
            for runtime in inventory.runtimes
            if runtime.key.stable_id() == runtime_id
        )
        if len(matches) != 1:
            raise AutoRoutingServiceError(
                "inherited safe default is absent from current inventory"
            )
        runtime = matches[0]
        requested = effective_generic_reasoning_effort(
            request.baseline.reasoning_config
        )
        default, supported = self._inherited_safe_default_reasoning(
            runtime.reasoning_support,
            requested,
        )
        return RoutingTarget(
            runtime=runtime.key,
            reasoning=ReasoningBounds.model_validate({
                "default": default,
                "min": supported[0],
                "max": supported[-1],
            }),
            supported_reasoning_efforts=supported,
            revision_status="fallback",
        )

    @staticmethod
    def _runtime_current_target(target: RoutingTarget, inventory: Any) -> RoutingTarget:
        matches = tuple(
            runtime
            for runtime in inventory.runtimes
            if runtime.key.stable_id() == target.runtime.stable_id()
        )
        if len(matches) != 1:
            return target
        runtime = matches[0]
        supported = tuple(
            effort
            for effort in REASONING_EFFORT_ORDER
            if effort in runtime.reasoning_support.efforts
        )
        return target.model_copy(
            update={
                "runtime": runtime.key,
                "supported_reasoning_efforts": supported,
            }
        )

    def _runtime_current_profiles(
        self,
        config: AutoRoutingConfig,
        inventory: Any,
    ) -> tuple[RouteProfile, ...]:
        return tuple(
            profile.model_copy(
                update={
                    "primary": self._runtime_current_target(
                        profile.primary,
                        inventory,
                    ),
                    "primary_challengers": tuple(
                        self._runtime_current_target(target, inventory)
                        for target in profile.primary_challengers
                    ),
                    "fallbacks": tuple(
                        self._runtime_current_target(target, inventory)
                        for target in profile.fallbacks
                    ),
                }
            )
            for profile in config.profiles.values()
        )

    def _runtime_rule_evaluation(
        self,
        *,
        config: AutoRoutingConfig,
        request: AgentRuntimeRequest,
        facts: Any,
        inventory: Any,
    ) -> Any:
        deterministic = evaluate_rules(
            config.rules,
            facts=facts,
            assessment=None,
            vocabulary=config.routing_vocabulary,
        )
        if deterministic.is_complete or deterministic.safe_default_reason is not None:
            return deterministic
        matches = tuple(
            runtime
            for runtime in inventory.runtimes
            if runtime.key.provider == config.classifier.provider
            and runtime.key.model == config.classifier.model
            and runtime.state == "verified"
        )
        if len(matches) != 1:
            return replace(
                deterministic,
                safe_default_reason="classifier_failed",
            )
        classifier = StructuredTaskClassifier(
            settings=config.classifier,
            policy=config.policy,
            vocabulary=config.routing_vocabulary,
            runtime=matches[0],
            store=self.store,
            llm=self.plugin_context.llm,
            now=lambda: datetime.now(UTC),
        )
        return assess_with_rules(
            config.rules,
            task=request.context.task,
            facts=facts,
            classifier=classifier,
            vocabulary=config.routing_vocabulary,
        )

    def _runtime_failed_selection(
        self,
        *,
        reason: str,
        safe_default: RoutingTarget,
        inventory: Any,
        config: AutoRoutingConfig,
        catalog: CatalogService,
    ) -> SelectionResult:
        matches = tuple(
            runtime
            for runtime in inventory.runtimes
            if runtime.key.stable_id() == safe_default.runtime.stable_id()
        )
        if len(matches) != 1 or matches[0].state != "verified":
            raise AutoRoutingServiceError("safe default is not currently verified")
        runtime = matches[0]
        if runtime_policy_rejection_reasons(
            runtime,
            policy=config.policy,
            catalog=catalog,
        ):
            raise AutoRoutingServiceError("safe default violates immutable policy")
        supported = tuple(safe_default.supported_reasoning_efforts)
        effort = safe_default.reasoning.default
        if effort not in supported:
            raise AutoRoutingServiceError("safe default reasoning is unsupported")
        runtime_id = runtime.key.stable_id()
        candidate = DecisionCandidate(
            candidate_id=candidate_id_for(
                "safe-default",
                "safe_default",
                0,
                runtime_id,
            ),
            profile_id="safe-default",
            target_role="safe_default",
            target_ordinal=0,
            runtime_id=runtime_id,
            eligible=True,
            reason_codes=(),
            normalized_scoring_inputs=(),
            final_score=None,
        )
        return SelectionResult(
            assessment=None,
            candidates=(candidate,),
            eligible_runtime_ids=(),
            rejections={},
            score_calls=(),
            selected_profile_id=None,
            selected_runtime=runtime,
            selected_reasoning_effort=effort,
            fallbacks=(),
            safe_default_runtime=runtime,
            safe_default_reasoning_effort=effort,
            selection_reason="safe_default",
            safe_default_reason=reason,
        )

    @staticmethod
    def _runtime_resolution_failed_candidates(
        selection: SelectionResult,
        failed_candidate_ids: set[str],
    ) -> SelectionResult:
        candidates = tuple(
            candidate.model_copy(
                update={
                    "eligible": False,
                    "reason_codes": tuple(
                        dict.fromkeys((
                            *candidate.reason_codes,
                            "runtime_resolution_failed",
                        ))
                    ),
                }
            )
            if candidate.candidate_id in failed_candidate_ids
            else candidate
            for candidate in selection.candidates
        )
        eligible_runtime_ids = tuple(
            dict.fromkeys(
                candidate.runtime_id
                for candidate in candidates
                if candidate.eligible
            )
        )
        rejected: dict[str, tuple[str, ...]] = {}
        eligible = set(eligible_runtime_ids)
        for candidate in candidates:
            if candidate.eligible or candidate.runtime_id in eligible:
                continue
            rejected.setdefault(candidate.runtime_id, candidate.reason_codes)
        return replace(
            selection,
            candidates=candidates,
            eligible_runtime_ids=eligible_runtime_ids,
            rejections=rejected,
        )

    @staticmethod
    def _baseline_runtime_key(request: AgentRuntimeRequest) -> RuntimeKey:
        baseline = request.baseline
        record = {
            "provider": baseline.provider or "hermes-default",
            "model": baseline.model or "hermes-default",
            "api_mode": baseline.api_mode or "hermes-default",
            "resolution_state": baseline.resolution_state,
            "resolution_reason_code": baseline.resolution_reason_code,
        }
        fingerprint = _checksum(_canonical_json(record))
        return RuntimeKey(
            provider=record["provider"],
            model=record["model"],
            auth_identity=f"baseline:{fingerprint[:32]}",
            credential_pool_identity=f"baseline:{fingerprint[:32]}",
            endpoint_identity=f"baseline:{fingerprint[:32]}",
            api_mode=record["api_mode"],
            local_backend="",
            inventory_revision=f"baseline:{fingerprint[:32]}",
        )

    def _runtime_resolve_pre_call(
        self,
        *,
        selection: SelectionResult,
        inventory: Any,
        request: AgentRuntimeRequest,
        hermes_config: Mapping[str, Any],
    ) -> tuple[SelectionResult, AgentRuntimeSpec | None]:
        """Resolve the recorded Auto chain before any provider client exists."""
        current = {
            runtime.key.stable_id(): runtime for runtime in inventory.runtimes
        }
        selected_id = selection.selected_runtime.key.stable_id()
        selected_candidate = next(
            (
                candidate
                for candidate in selection.candidates
                if candidate.profile_id == selection.selected_profile_id
                and candidate.runtime_id == selected_id
                and candidate.eligible
            ),
            None,
        )
        chain: list[tuple[Any, str, str, int | None, str | None]] = [
            (
                selection.selected_runtime,
                selection.selected_reasoning_effort,
                "selected",
                None,
                None if selected_candidate is None else selected_candidate.candidate_id,
            )
        ]
        for index, target in enumerate(selection.fallbacks):
            runtime_id = target.runtime.stable_id()
            runtime = current.get(runtime_id)
            if runtime is None:
                continue
            candidate = next(
                (
                    item
                    for item in selection.candidates
                    if item.profile_id == selection.selected_profile_id
                    and item.target_role == "fallback"
                    and item.runtime_id == runtime_id
                    and item.eligible
                ),
                None,
            )
            chain.append((
                runtime,
                target.reasoning.default,
                "fallback",
                index,
                None if candidate is None else candidate.candidate_id,
            ))
        safe_candidate = next(
            (
                item
                for item in selection.candidates
                if item.profile_id == "safe-default"
                and item.target_role == "safe_default"
            ),
            None,
        )
        chain.append((
            selection.safe_default_runtime,
            selection.safe_default_reasoning_effort,
            "safe_default",
            None,
            None if safe_candidate is None else safe_candidate.candidate_id,
        ))

        failed_candidate_ids: set[str] = set()
        for runtime, effort, role, fallback_index, candidate_id in chain:
            try:
                resolved = self.adapter.resolve(runtime.key)
                spec = self.adapter.to_agent_runtime_spec(
                    resolved,
                    reasoning_effort=effort,
                    hermes_config=hermes_config,
                )
            except Exception:
                if candidate_id is not None:
                    failed_candidate_ids.add(candidate_id)
                continue

            updated = self._runtime_resolution_failed_candidates(
                selection,
                failed_candidate_ids,
            )
            if role == "fallback":
                assert fallback_index is not None
                updated = replace(
                    updated,
                    selected_runtime=runtime,
                    selected_reasoning_effort=effort,
                    fallbacks=selection.fallbacks[fallback_index + 1 :],
                    selection_reason="pre_call_fallback",
                )
            elif role == "safe_default":
                updated = replace(
                    updated,
                    selected_profile_id=None,
                    selected_runtime=runtime,
                    selected_reasoning_effort=effort,
                    fallbacks=(),
                    selection_reason="safe_default",
                )
            return updated, spec

        baseline_key = self._baseline_runtime_key(request)
        requested_effort = effective_generic_reasoning_effort(
            request.baseline.reasoning_config
        )
        if requested_effort not in REASONING_EFFORT_ORDER:
            requested_effort = "none"
        baseline_runtime = replace(
            selection.selected_runtime,
            key=baseline_key,
        )
        return (
            SelectionResult(
                assessment=None,
                candidates=(),
                eligible_runtime_ids=(),
                rejections={},
                score_calls=(),
                selected_profile_id=None,
                selected_runtime=baseline_runtime,
                selected_reasoning_effort=requested_effort,
                fallbacks=(),
                safe_default_runtime=baseline_runtime,
                safe_default_reasoning_effort=requested_effort,
                selection_reason="baseline_inherit",
                safe_default_reason="safe_default_unavailable",
            ),
            None,
        )

    def create_runtime_decision(
        self,
        *,
        request: AgentRuntimeRequest,
        config: AutoRoutingConfig,
        activation_receipt: Any,
        adapter_capability_sha: str,
    ) -> AgentRuntimePlan:
        """Compute, commit, and project one fresh semantic decision."""
        if config.activation.mode == "active":
            current_authority_id = authority_revision(config)
            if not self._authority_is_usable(config, current_authority_id):
                raise AutoRoutingServiceError(
                    "active routing authority baseline is unavailable"
                )
            expected_receipt = self.store.read_matching_activation_receipt(
                authority_id=current_authority_id,
                config_sha=config_revision(config),
                adapter_capability_sha=adapter_capability_sha,
            )
            if (
                activation_receipt is None
                or expected_receipt is None
                or activation_receipt != expected_receipt
            ):
                raise AutoRoutingServiceError(
                    "active routing requires the current matching activation receipt"
                )
        started = time.monotonic()
        metadata = request.context.metadata
        platform = str(metadata.get("platform") or "unknown")
        facts = extract_task_facts(
            scope=request.context.scope,
            task=request.context.task,
            metadata=self._runtime_fact_metadata(metadata),
            platform=platform,
        )
        facts_hash = task_facts_hash(facts)
        claim = self.store.claim_decision_operation(
            scope=request.context.scope,
            session_id=request.context.session_id,
            operation_id=request.context.operation_id,
            task_index=request.context.task_index,
            facts_hash=facts_hash,
            lease_seconds=max(5.0, float(config.classifier.timeout_seconds) + 5.0),
        )
        if claim.status == "replayed":
            decision = self.store.read_decision(str(claim.decision_id))
            binding = self.store.read_session_binding(request.context.session_id)
            if decision is None or binding is None:
                raise AutoRoutingServiceError("replayed decision is incomplete")
            return self.replay_runtime_decision(
                request=request,
                binding=binding,
            )
        if claim.status == "waiting":
            decision = self.store.wait_for_decision_operation(
                claim,
                timeout_seconds=0.25,
            )
            if decision is None:
                raise RuntimeRoutingPending(
                    "another process owns the route decision operation"
                )
            binding = self.store.read_session_binding(request.context.session_id)
            if binding is None:
                raise AutoRoutingServiceError("replayed decision binding is missing")
            return self.replay_runtime_decision(
                request=request,
                binding=binding,
            )

        inventory = self._new_inventory_service(policy=config.policy).refresh(
            refresh=False,
            persist=True,
        )
        evaluation = self._runtime_rule_evaluation(
            config=config,
            request=request,
            facts=facts,
            inventory=inventory,
        )
        catalog = CatalogService(store=self.store)
        safe_default = self._runtime_safe_default_target(config, inventory, request)
        authority_id = authority_revision(config)
        adaptive_profile_snapshot: dict[str, str] = {}
        profile_adaptive_revision_id: str | None = None
        adaptive_assignment_id: str | None = None
        pending_canary_assignment: AdaptiveCanaryAssignment | None = None
        control_snapshot: dict[str, str] | None = None
        if evaluation.assessment is None:
            selection = self._runtime_failed_selection(
                reason=evaluation.safe_default_reason or "classifier_failed",
                safe_default=safe_default,
                inventory=inventory,
                config=config,
                catalog=catalog,
            )
        else:
            if evaluation.assessment.risk_class not in {"high", "critical"}:
                for profile in sorted(
                    config.profiles.values(), key=lambda item: item.profile_id
                ):
                    if profile.adaptation.enabled:
                        self.maybe_advance_adaptation(profile_id=profile.profile_id)
            adaptive_profile_snapshot = self.resolve_effective_adaptation_snapshot(
                authority_id=authority_id,
                profile_ids=tuple(config.profiles),
            )
            hermes_config = self._runtime_root_config()
            requested_effort = effective_generic_reasoning_effort(
                request.baseline.reasoning_config
            )
            def select_snapshot(snapshot: Mapping[str, str]) -> SelectionResult:
                return StaticSelector(
                    catalog=catalog,
                    now=lambda: datetime.now(UTC),
                ).select(
                    profiles=self._profiles_for_adaptation_snapshot(
                        config,
                        snapshot,
                        inventory,
                    ),
                    assessment=evaluation.assessment,
                    inventory=inventory,
                    policy=config.policy,
                    complexity_bands=config.complexity_bands,
                    safe_default=safe_default,
                    requested_reasoning_effort=requested_effort,
                    pinned_profile_id=evaluation.profile_id,
                    preferred_profile_id=(
                        evaluation.preferred_profile_ids[0]
                        if evaluation.preferred_profile_ids
                        else None
                    ),
                    hermes_config=hermes_config,
                    task_definition=str(
                        metadata.get("task_definition") or "default"
                    ),
                )

            control_snapshot = dict(adaptive_profile_snapshot)
            selection = select_snapshot(control_snapshot)
            context_bucket_id = build_context_bucket(
                evaluation.assessment,
                config.complexity_bands,
            ).bucket_id
            provisional = self._provisional_canary_assignment(
                config=config,
                request=request,
                assessment=evaluation.assessment,
                inventory=inventory,
                selected_profile_id=selection.selected_profile_id,
                context_bucket_id=context_bucket_id,
            )
            if provisional is not None:
                armed_revision_id = (
                    provisional.challenger_revision_id
                    if provisional.arm == "challenger"
                    else provisional.control_revision_id
                )
                armed_snapshot = dict(control_snapshot)
                armed_snapshot[provisional.profile_id] = armed_revision_id
                armed_selection = select_snapshot(armed_snapshot)
                if self._canary_assignment_matches_final_route(
                    provisional,
                    selected_profile_id=armed_selection.selected_profile_id,
                    selected_revision_id=(
                        armed_snapshot.get(armed_selection.selected_profile_id)
                        if armed_selection.selected_profile_id is not None
                        else None
                    ),
                    selected_runtime_id=armed_selection.selected_runtime.key.stable_id(),
                    selected_reasoning_effort=(
                        armed_selection.selected_reasoning_effort
                    ),
                ):
                    adaptive_profile_snapshot = armed_snapshot
                    selection = armed_selection
                    # Do not write the arm yet. Active routes can still change
                    # at the pre-call resolver, so only the post-resolution
                    # selection may become durable canary evidence.
                    pending_canary_assignment = provisional
                else:
                    adaptive_profile_snapshot = control_snapshot
            if selection.selected_profile_id is not None:
                profile_adaptive_revision_id = adaptive_profile_snapshot[
                    selection.selected_profile_id
                ]

        projected_spec = None
        projection_mode = config.activation.mode
        stage4_canary_attempted = pending_canary_assignment is not None
        final_resolution_attempted = False
        if projection_mode == "active" and stage4_canary_attempted:
            selection, projected_spec = self._runtime_resolve_pre_call(
                selection=selection,
                inventory=inventory,
                request=request,
                hermes_config=self._runtime_root_config(),
            )
            final_resolution_attempted = True

        if pending_canary_assignment is not None and selection.selected_profile_id is not None:
            final_arm_matches = self._canary_assignment_matches_final_route(
                pending_canary_assignment,
                selected_profile_id=selection.selected_profile_id,
                selected_revision_id=(
                    adaptive_profile_snapshot.get(selection.selected_profile_id)
                    if adaptive_profile_snapshot
                    else None
                ),
                selected_runtime_id=selection.selected_runtime.key.stable_id(),
                selected_reasoning_effort=selection.selected_reasoning_effort,
            )
            if final_arm_matches:
                persisted = self._persist_matching_canary_assignment(
                    pending_canary_assignment,
                    selected_profile_id=selection.selected_profile_id,
                    selected_revision_id=(
                        adaptive_profile_snapshot.get(selection.selected_profile_id)
                        if adaptive_profile_snapshot
                        else None
                    ),
                    selected_runtime_id=selection.selected_runtime.key.stable_id(),
                    selected_reasoning_effort=selection.selected_reasoning_effort,
                )
                if persisted is not None:
                    adaptive_assignment_id = persisted.assignment_id
                else:
                    # A frozen/stale lifecycle rejected the durable arm after
                    # selection. Never send challenger traffic without the
                    # matching persisted assignment that gates its evidence.
                    if control_snapshot is None:  # pragma: no cover - invariant
                        raise AutoRoutingServiceError(
                            "canary control snapshot is unavailable"
                        )
                    pending_canary_assignment = None
                    adaptive_assignment_id = None
                    adaptive_profile_snapshot = dict(control_snapshot)
                    selection = select_snapshot(adaptive_profile_snapshot)
                    if config.activation.mode == "active":
                        selection, projected_spec = self._runtime_resolve_pre_call(
                            selection=selection,
                            inventory=inventory,
                            request=request,
                            hermes_config=self._runtime_root_config(),
                        )
                        projection_mode = (
                            "active" if projected_spec is not None else "inherit"
                        )
                    if selection.selected_profile_id is None:
                        adaptive_profile_snapshot = {}
                        profile_adaptive_revision_id = None
                    else:
                        profile_adaptive_revision_id = (
                            adaptive_profile_snapshot[
                                selection.selected_profile_id
                            ]
                        )

        management_snapshot = ManagementDecisionSnapshot()
        if projection_mode == "active" and not stage4_canary_attempted:
            if selection.selected_profile_id is not None:
                try:
                    self.maybe_advance_management(
                        profile_id=selection.selected_profile_id,
                    )
                except Exception:
                    pass
            selection, projected_spec, management_snapshot = (
                self._apply_management_runtime_overlay(
                    request=request,
                    config=config,
                    selection=selection,
                    inventory=inventory,
                    projected_spec=None,
                    adaptive_assignment_id=None,
                    now=datetime.now(UTC),
                )
            )
        if (
            projection_mode == "active"
            and projected_spec is None
            and not final_resolution_attempted
        ):
            selection, projected_spec = self._runtime_resolve_pre_call(
                selection=selection,
                inventory=inventory,
                request=request,
                hermes_config=self._runtime_root_config(),
            )
        if projection_mode == "active" and projected_spec is None:
            projection_mode = "inherit"

        if selection.selected_profile_id is None:
            adaptive_profile_snapshot = {}
            profile_adaptive_revision_id = None
            adaptive_assignment_id = None
        elif adaptive_profile_snapshot:
            profile_adaptive_revision_id = adaptive_profile_snapshot[
                selection.selected_profile_id
            ]

        self.store.write_authority_revision(
            authority_id,
            authority_document(config),
            created_at=self._runtime_timestamp(),
        )
        catalog_id = (
            catalog.snapshot.snapshot_id
            if catalog.snapshot is not None
            else "catalog-unavailable"
        )
        adaptive = self.store.read_active_revision(authority_id)
        adaptive_id = (
            adaptive.revision_id
            if adaptive is not None
            else f"static-{authority_id[:32]}"
        )
        receipt_id = None
        activation_config_sha = None
        if projection_mode == "active":
            if activation_receipt is None:
                raise AutoRoutingServiceError("active routing requires a receipt")
            receipt_id = activation_receipt.receipt_id
            activation_config_sha = activation_receipt.config_sha
        baseline_inherit = selection.selection_reason == "baseline_inherit"
        built = DecisionBuilder().build(
            scope=request.context.scope,
            session_id=request.context.session_id,
            task_id=request.context.task_id,
            operation_id=request.context.operation_id,
            task_index=request.context.task_index,
            selection=selection,
            task_facts_hash=facts_hash,
            inventory_revision=inventory.revision,
            catalog_revision=catalog_id,
            authority_revision=authority_id,
            policy_revision=self._runtime_revision(config.policy),
            adaptive_revision=adaptive_id,
            profile_adaptive_revision_id=profile_adaptive_revision_id,
            adaptive_assignment_id=adaptive_assignment_id,
            adaptive_profile_snapshot=adaptive_profile_snapshot,
            management_revision_id=(
                management_snapshot.management_revision_id
            ),
            management_assignment_id=(
                management_snapshot.management_assignment_id
            ),
            management_profile_snapshot=(
                management_snapshot.management_profile_snapshot
            ),
            projection_mode=projection_mode,
            routing_latency_seconds=max(0.0, time.monotonic() - started),
            applied_rule_ids=(
                () if baseline_inherit else evaluation.applied_rule_ids
            ),
            activation_receipt_id=receipt_id,
            activation_config_sha=activation_config_sha,
            adapter_capability_sha=(
                adapter_capability_sha if projection_mode == "active" else None
            ),
            classifier_runtime_id=(
                None if baseline_inherit else evaluation.classifier_runtime_id
            ),
            classifier_input_tokens=(
                0 if baseline_inherit else evaluation.classifier_input_tokens
            ),
            classifier_output_tokens=(
                0 if baseline_inherit else evaluation.classifier_output_tokens
            ),
            classifier_cost_usd=(
                None if baseline_inherit else evaluation.classifier_cost_usd
            ),
        )

        committed = self.store.commit_decision(
            built.decision,
            candidates=built.candidates,
            create_epoch=projection_mode == "active",
            claim=claim,
        )
        event = {
            "decision_id": committed.decision.decision_id,
            "runtime_id": committed.binding.runtime_id,
            "projection_mode": projection_mode,
            "profile_adaptive_revision_id": (
                committed.decision.profile_adaptive_revision_id
            ),
            "adaptive_assignment_id": committed.decision.adaptive_assignment_id,
        }
        management_revision_id = getattr(
            committed.decision,
            "management_revision_id",
            None,
        )
        management_assignment_id = getattr(
            committed.decision,
            "management_assignment_id",
            None,
        )
        if management_revision_id is not None or management_assignment_id is not None:
            event.update({
                "management_revision_id": management_revision_id,
                "management_assignment_id": management_assignment_id,
            })
        if projection_mode == "shadow":
            return AgentRuntimePlan(
                action="shadow",
                runtime=request.baseline,
                decision_id=committed.decision.decision_id,
                bound_route_identity=built.semantic_checksum,
                owns_fallbacks=False,
                reason_code="shadow_recorded",
                event=event,
            )
        if projection_mode == "inherit":
            return AgentRuntimePlan(
                action="inherit",
                runtime=request.baseline,
                decision_id=committed.decision.decision_id,
                bound_route_identity=built.semantic_checksum,
                owns_fallbacks=False,
                reason_code="baseline_inherit",
                event=event,
            )
        assert projected_spec is not None
        event["post_call_model_failover"] = False
        return AgentRuntimePlan(
            action="project",
            runtime=projected_spec,
            decision_id=committed.decision.decision_id,
            bound_route_identity=built.semantic_checksum,
            owns_fallbacks=True,
            reason_code="active_projected",
            event=event,
        )

    @staticmethod
    def _validate_recorded_overlay_authority(
        overlay: AdaptiveOverlay,
        profile_document: Any,
    ) -> None:
        """Validate an overlay against the canonical recorded authority document."""
        if not isinstance(profile_document, Mapping):
            raise ValueError("recorded profile authority is invalid")
        targets = [profile_document.get("primary")]
        challengers = profile_document.get("primary_challengers", ())
        if not isinstance(challengers, (list, tuple)):
            raise ValueError("recorded challenger authority is invalid")
        targets.extend(challengers)
        bounds: dict[str, tuple[int, int]] = {}
        for target in targets:
            if not isinstance(target, Mapping):
                raise ValueError("recorded primary authority is invalid")
            runtime_value = target.get("runtime")
            reasoning = target.get("reasoning")
            if not isinstance(runtime_value, Mapping) or not isinstance(
                reasoning, Mapping
            ):
                raise ValueError("recorded target authority is invalid")
            runtime = RuntimeKey.model_validate({
                **runtime_value,
                "inventory_revision": "recorded-authority",
            })
            minimum = REASONING_EFFORT_ORDER.index(str(reasoning.get("minimum")))
            maximum = REASONING_EFFORT_ORDER.index(str(reasoning.get("maximum")))
            bounds[runtime.stable_id()] = (minimum, maximum)
        if (
            set(overlay.ordered_primary_runtime_ids) != set(bounds)
            or len(overlay.ordered_primary_runtime_ids) != len(bounds)
        ):
            raise ValueError("recorded overlay escapes primary authority")
        for runtime_id, effort in overlay.reasoning_defaults.items():
            position = REASONING_EFFORT_ORDER.index(effort)
            minimum, maximum = bounds[runtime_id]
            if not minimum <= position <= maximum:
                raise ValueError("recorded overlay effort escapes authority")

    def _validate_recorded_runtime_decision(
        self,
        decision: Any,
        binding: Any,
    ) -> None:
        """Verify every immutable document named by a replayed decision."""
        if binding.projection_mode != decision.projection_mode:
            raise AutoRoutingServiceError("recorded route binding mode changed")

        authority = self.store.read_authority_revision(decision.authority_revision)
        if authority is None:
            raise AutoRoutingServiceError("recorded authority revision is unavailable")
        try:
            authority_value = json.loads(authority.document_json)
        except Exception as error:  # pragma: no cover - store verifies first
            raise AutoRoutingServiceError(
                "recorded authority revision is invalid"
            ) from error
        authority_id = hashlib.sha256(
            json.dumps(
                authority_value,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if authority_id != decision.authority_revision:
            raise AutoRoutingServiceError("recorded authority identity changed")
        policy_value = authority_value.get("policy")
        if self._runtime_revision(policy_value) != decision.policy_revision:
            raise AutoRoutingServiceError("recorded policy revision changed")

        if decision.adaptive_profile_snapshot:
            recorded_profiles = authority_value.get("profiles")
            if not isinstance(recorded_profiles, Mapping):
                raise AutoRoutingServiceError("recorded adaptive authority is invalid")
            if set(decision.adaptive_profile_snapshot) != set(
                recorded_profiles
            ):
                raise AutoRoutingServiceError(
                    "recorded adaptive profile snapshot is incomplete"
                )
            if decision.selected_profile_id is None:
                raise AutoRoutingServiceError(
                    "recorded adaptive snapshot has no selected profile"
                )
            selected_revision_id = decision.adaptive_profile_snapshot[
                decision.selected_profile_id
            ]
            if selected_revision_id != decision.profile_adaptive_revision_id:
                raise AutoRoutingServiceError(
                    "recorded selected profile revision changed"
                )
            for profile_id, revision_id in decision.adaptive_profile_snapshot.items():
                static_id = self.static_profile_revision_id(
                    decision.authority_revision,
                    profile_id,
                )
                if revision_id == static_id:
                    continue
                revision = self.store.read_profile_revision(revision_id)
                if (
                    revision is None
                    or not revision.complete
                    or revision.authority_id != decision.authority_revision
                    or revision.profile_id != profile_id
                ):
                    raise AutoRoutingServiceError(
                        "recorded profile adaptive revision is unavailable"
                    )
                try:
                    self._validate_recorded_overlay_authority(
                        revision.overlay,
                        recorded_profiles[profile_id],
                    )
                except Exception as error:
                    raise AutoRoutingServiceError(
                        "recorded profile adaptive revision is invalid"
                    ) from error
            if decision.adaptive_assignment_id is not None:
                assignment = self.store.read_canary_assignment_by_id(
                    decision.adaptive_assignment_id
                )
                expected_assignment_revision = (
                    None
                    if assignment is None
                    else (
                        assignment.challenger_revision_id
                        if assignment.arm == "challenger"
                        else assignment.control_revision_id
                    )
                )
                assignment_revision = (
                    None
                    if expected_assignment_revision is None
                    else self.store.read_profile_revision(expected_assignment_revision)
                )
                expected_runtime = (
                    None
                    if assignment_revision is None
                    else assignment_revision.overlay.ordered_primary_runtime_ids[0]
                )
                expected_effort = (
                    None
                    if assignment_revision is None or expected_runtime is None
                    else assignment_revision.overlay.reasoning_defaults.get(
                        expected_runtime
                    )
                )
                if (
                    assignment is None
                    or assignment.authority_id != decision.authority_revision
                    or assignment.profile_id != decision.selected_profile_id
                    or selected_revision_id
                    not in {
                        assignment.control_revision_id,
                        assignment.challenger_revision_id,
                    }
                    or selected_revision_id != expected_assignment_revision
                    or decision.selected_runtime.stable_id() != expected_runtime
                    or decision.selected_reasoning_effort != expected_effort
                ):
                    raise AutoRoutingServiceError(
                        "recorded adaptive assignment is unavailable"
                    )

        if decision.management_profile_snapshot:
            if decision.selected_profile_id is None:
                raise AutoRoutingServiceError(
                    "recorded management snapshot has no selected profile"
                )
            selected_management_revision_id = (
                decision.management_profile_snapshot.get(
                    decision.selected_profile_id
                )
            )
            selected_management_revision = (
                None
                if selected_management_revision_id is None
                else self.store.read_management_revision(
                    selected_management_revision_id
                )
            )
            if (
                selected_management_revision_id
                != decision.management_revision_id
                or selected_management_revision is None
                or self._management_revision_patch(
                    selected_management_revision,
                    decision.selected_profile_id,
                )
                is None
            ):
                raise AutoRoutingServiceError(
                    "recorded management revision is unavailable"
                )
            if decision.management_assignment_id is not None:
                assignment = self.store.read_management_assignment(
                    decision.management_assignment_id
                )
                expected_revision_id = (
                    None
                    if assignment is None
                    else assignment.challenger_revision_id
                    if assignment.arm == "challenger"
                    else assignment.control_revision_id
                )
                if (
                    assignment is None
                    or assignment.phase not in {"finalized", "terminal"}
                    or assignment.management_authority_id
                    != selected_management_revision.management_authority_id
                    or assignment.profile_id != decision.selected_profile_id
                    or expected_revision_id != selected_management_revision_id
                    or assignment.runtime_id
                    != decision.selected_runtime.stable_id()
                    or assignment.reasoning_effort
                    != decision.selected_reasoning_effort
                ):
                    raise AutoRoutingServiceError(
                        "recorded management assignment is unavailable"
                    )

        static_revision = f"static-{decision.authority_revision[:32]}"
        if decision.adaptive_revision != static_revision:
            adaptive = self.store.read_revision(decision.adaptive_revision)
            if (
                adaptive is None
                or adaptive.authority_id != decision.authority_revision
            ):
                raise AutoRoutingServiceError(
                    "recorded adaptive revision is unavailable"
                )

        if self.store.read_inventory_snapshot(decision.inventory_revision) is None:
            raise AutoRoutingServiceError(
                "recorded inventory revision is unavailable"
            )
        if (
            decision.catalog_revision != "catalog-unavailable"
            and self.store.read_catalog_snapshot(decision.catalog_revision) is None
        ):
            raise AutoRoutingServiceError("recorded catalog revision is unavailable")

        if decision.projection_mode == "active":
            if (
                decision.activation_receipt_id is None
                or decision.activation_config_sha is None
                or decision.adapter_capability_sha is None
            ):
                raise AutoRoutingServiceError(
                    "recorded activation receipt identity is incomplete"
                )
            receipt = self.store.read_activation_receipt(
                decision.activation_receipt_id
            )
            if (
                receipt is None
                or receipt.authority_id != decision.authority_revision
                or receipt.config_sha != decision.activation_config_sha
                or receipt.adapter_capability_sha
                != decision.adapter_capability_sha
            ):
                raise AutoRoutingServiceError(
                    "recorded activation receipt is unavailable"
                )

    def replay_runtime_decision(
        self,
        *,
        request: AgentRuntimeRequest,
        binding: Any,
    ) -> AgentRuntimePlan:
        """Replay only the immutable recorded runtime chain."""
        if binding.decision_id is None:
            raise AutoRoutingServiceError("routed binding has no decision")
        decision = self.store.read_decision(binding.decision_id)
        is_origin_session = (
            decision is not None and decision.session_id == binding.session_id
        )
        is_compression_descendant = (
            decision is not None
            and binding.continuation_reason == "compression"
            and binding.parent_session_id is not None
            and binding.continuation_root == decision.session_id
        )
        if decision is None or not (
            is_origin_session or is_compression_descendant
        ):
            raise AutoRoutingServiceError("recorded route decision is unavailable")
        self._validate_recorded_runtime_decision(decision, binding)
        if decision.projection_mode == "shadow":
            return AgentRuntimePlan(
                action="shadow",
                runtime=request.baseline,
                decision_id=decision.decision_id,
                bound_route_identity=decision.decision_id,
                owns_fallbacks=False,
                reason_code="shadow_recorded",
                event={
                    "decision_id": decision.decision_id,
                    "projection_mode": "shadow",
                    "profile_adaptive_revision_id": (
                        decision.profile_adaptive_revision_id
                    ),
                    "adaptive_assignment_id": decision.adaptive_assignment_id,
                },
            )
        if decision.projection_mode == "inherit":
            return AgentRuntimePlan(
                action="inherit",
                runtime=request.baseline,
                decision_id=decision.decision_id,
                bound_route_identity=decision.decision_id,
                owns_fallbacks=False,
                reason_code="baseline_inherit",
                event={
                    "decision_id": decision.decision_id,
                    "projection_mode": "inherit",
                    "profile_adaptive_revision_id": (
                        decision.profile_adaptive_revision_id
                    ),
                    "adaptive_assignment_id": decision.adaptive_assignment_id,
                },
            )

        inventory = InventoryService(
            self.adapter,
            store=self.store,
            policy=None,
        ).refresh(
            refresh=False,
            persist=False,
        )
        current = {
            runtime.key.stable_id(): runtime
            for runtime in inventory.runtimes
        }
        chain = [
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
        selected_runtime = None
        selected_effort = None
        resolved = None
        for runtime_key, effort in chain:
            runtime = current.get(runtime_key.stable_id())
            if runtime is None or runtime.state != "verified":
                continue
            try:
                candidate = self.adapter.resolve(runtime.key)
            except Exception:
                continue
            selected_runtime = runtime
            selected_effort = effort
            resolved = candidate
            break
        if selected_runtime is None or selected_effort is None or resolved is None:
            raise AutoRoutingServiceError("recorded runtime chain is unavailable")

        cache_degraded = False
        if selected_runtime.key.stable_id() != binding.runtime_id:
            epochs = self.store.read_route_epochs(binding.session_id)
            current_epoch = next(
                (
                    epoch
                    for epoch in epochs
                    if epoch.epoch_number == binding.current_epoch
                ),
                None,
            )
            cache_degraded = bool(
                current_epoch is not None and current_epoch.provider_started
            )
            if (
                not cache_degraded
                and request.context.is_resume
                and request.context.metadata.get("proven_pre_dispatch_crash") is not True
            ):
                # The observer is deliberately non-vetoing. On recovery, an
                # absent marker is therefore not proof that dispatch never
                # happened; only explicit durable pre-dispatch evidence may
                # preserve that claim.
                cache_degraded = True
            self.store.start_route_epoch(
                session_id=binding.session_id,
                decision_id=decision.decision_id,
                runtime_id=selected_runtime.key.stable_id(),
                reason_code="recorded_fallback",
                started_at=self._runtime_timestamp(),
                expected_epoch=binding.current_epoch,
            )
        project_recorded = getattr(
            self.adapter,
            "to_recorded_agent_runtime_spec",
            None,
        )
        if not callable(project_recorded):
            raise AutoRoutingServiceError(
                "adapter cannot project a recorded runtime binding"
            )
        spec = project_recorded(
            resolved,
            # The recorded provider-independent effort is authoritative on
            # replay. A newer or corrupt config must not alter or block it.
            reasoning_effort=selected_effort,
        )
        return AgentRuntimePlan(
            action="project",
            runtime=spec,
            decision_id=decision.decision_id,
            bound_route_identity=decision.decision_id,
            owns_fallbacks=True,
            reason_code="active_projected",
            event={
                "decision_id": decision.decision_id,
                "runtime_id": selected_runtime.key.stable_id(),
                "cache_degraded": cache_degraded,
                "recorded_replay": True,
                "post_call_model_failover": False,
                "profile_adaptive_revision_id": (
                    decision.profile_adaptive_revision_id
                ),
                "adaptive_assignment_id": decision.adaptive_assignment_id,
            },
        )

    def record_runtime_manual_pin(self, request: ManualRuntimePinRequest) -> None:
        runtime_id = _checksum(_canonical_json(request.runtime.public_record()))
        self.store.record_manual_pin(
            request.session_id,
            runtime_id,
            request.source,
            self._runtime_timestamp(),
        )

    def record_runtime_continuation(
        self,
        request: RuntimeSessionContinuation,
    ) -> None:
        self.store.bind_session_continuation(
            request.parent_session_id,
            request.child_session_id,
            reason=request.reason,
            created_at=self._runtime_timestamp(),
        )

    def mark_runtime_provider_started(self, **event: Any) -> None:
        session_id = event.get("session_id")
        api_request_id = event.get("api_request_id")
        decision_id = event.get("decision_id")
        runtime_id = event.get("runtime_id")
        if not all(
            isinstance(value, str) and value
            for value in (
                session_id,
                api_request_id,
                decision_id,
                runtime_id,
            )
        ):
            return
        binding = self.store.read_session_binding(session_id)
        if (
            binding is None
            or binding.binding_kind != "routed"
            or binding.projection_mode != "active"
            or binding.decision_id != decision_id
            or binding.runtime_id != runtime_id
            or binding.current_epoch < 0
        ):
            return
        self.store.mark_route_epoch_provider_started(
            session_id,
            decision_id=decision_id,
            runtime_id=runtime_id,
            api_request_id=api_request_id,
            started_at=self._runtime_timestamp(),
        )

    @staticmethod
    def _recorded_target_for_runtime(
        decision: RoutingDecision,
        runtime_id: str,
    ) -> tuple[RuntimeKey, ReasoningEffort] | None:
        matches = [
            (runtime, effort)
            for runtime, effort in (
                (decision.selected_runtime, decision.selected_reasoning_effort),
                *(
                    (target.runtime, target.reasoning.default)
                    for target in decision.projected_fallback_chain
                ),
                (
                    decision.safe_default_runtime,
                    decision.safe_default_reasoning_effort,
                ),
            )
            if runtime.stable_id() == runtime_id
        ]
        if not matches:
            return None
        providers_and_models = {
            (runtime.provider, runtime.model) for runtime, _effort in matches
        }
        efforts = {effort for _runtime, effort in matches}
        if len(providers_and_models) != 1 or len(efforts) != 1:
            return None
        return matches[0][0], next(iter(efforts))

    def ingest_turn_outcome(
        self,
        payload: Mapping[str, Any],
    ) -> EvidenceCommit | None:
        """Persist one turn only when durable route attribution is exact."""
        self._assert_profile_isolation()
        observed = TurnOutcomeObserverPayload.model_validate(payload)
        public = observed.runtime_binding
        if (
            public is None
            or public.action != "project"
            or public.decision_id is None
            or observed.api_calls == 0
            or public.session_id != observed.session_id
            or public.task_id != observed.task_id
        ):
            return None
        binding = self.store.read_session_binding(observed.session_id)
        if (
            binding is None
            or binding.session_id != observed.session_id
            or binding.binding_kind != "routed"
            or binding.projection_mode != "active"
            or binding.decision_id != public.decision_id
            or binding.current_epoch < 0
        ):
            return None
        decision = self.store.read_decision(binding.decision_id)
        if (
            decision is None
            or decision.projection_mode != "active"
            or public.scope != decision.scope
        ):
            return None
        origin = decision.session_id == binding.session_id
        descendant = (
            binding.continuation_reason == "compression"
            and binding.parent_session_id is not None
            and binding.continuation_root == decision.session_id
        )
        if (
            not (origin or descendant)
            or (origin and observed.task_id != decision.task_id)
        ):
            return None
        epoch = next(
            (
                item
                for item in self.store.read_route_epochs(binding.session_id)
                if item.epoch_number == binding.current_epoch
            ),
            None,
        )
        if (
            epoch is None
            or epoch.decision_id != decision.decision_id
            or epoch.session_id != binding.session_id
            or epoch.runtime_id != binding.runtime_id
            or epoch.provider_started is not True
        ):
            return None
        recorded_target = self._recorded_target_for_runtime(
            decision,
            epoch.runtime_id,
        )
        if recorded_target is None:
            return None
        runtime, effort = recorded_target
        if (
            public.provider != runtime.provider
            or public.model != runtime.model
            or observed.reasoning_effort != effort
        ):
            return None
        is_initial = (
            observed.session_id == decision.session_id
            and observed.task_id == decision.task_id
        )
        context = None
        if is_initial and decision.assessment is not None:
            authority = self.store.read_authority_revision(
                decision.authority_revision
            )
            if authority is None:
                return None
            authority_payload = json.loads(authority.document_json)
            computed_authority_id = hashlib.sha256(
                json.dumps(
                    authority_payload,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if (
                authority.authority_id != decision.authority_revision
                or computed_authority_id != decision.authority_revision
            ):
                return None
            bands = ComplexityBands.model_validate(
                authority_payload["complexity_bands"]
            )
            context = build_context_bucket(decision.assessment, bands)
        normalized = normalize_turn_outcome(observed.outcome)
        event = EvidenceEvent(
            evidence_id=turn_evidence_id(
                observed.session_id,
                observed.turn_id,
            ),
            source="hermes_turn_outcome",
            signal_type=normalized.signal_type,
            decision_id=decision.decision_id,
            session_id=observed.session_id,
            turn_id=observed.turn_id,
            task_id=observed.task_id,
            route_epoch_id=epoch.route_epoch_id,
            runtime_id=epoch.runtime_id,
            profile_id=decision.selected_profile_id,
            reasoning_effort=effort,
            context_bucket=context,
            is_initial_routing_task=is_initial,
            outcome=observed.outcome,
            normalized_value=normalized.normalized_value,
            confidence_weight=normalized.confidence_weight,
            attribution_confidence=1.0,
            api_calls=observed.api_calls,
            tool_iterations=observed.tool_iterations,
            retry_count=observed.retry_count,
            cost_usd=observed.cost_usd,
            input_tokens=observed.input_tokens,
            output_tokens=observed.output_tokens,
            cache_read_tokens=observed.cache_read_tokens,
            latency_seconds=None,
            observed_at=datetime
            .fromtimestamp(
                observed.observed_at_unix,
                UTC,
            )
            .isoformat()
            .replace("+00:00", "Z"),
        )
        return self.store.write_observer_evidence_event(event)

    def record_feedback(
        self,
        *,
        evidence_id: str,
        value: EvidenceFeedbackValue,
    ) -> dict[str, Any]:
        """Append one finite feedback observation to routed turn evidence."""
        self._assert_profile_isolation()
        parent = self.store.read_evidence_event(evidence_id)
        if parent is None or parent.source != "hermes_turn_outcome":
            raise AutoRoutingServiceError(
                "feedback requires an existing routed turn evidence event"
            )
        event = build_feedback_event(
            parent,
            value,
            observed_at=self._runtime_timestamp(),
        )
        committed = self.store.write_evidence_event(event)
        return {
            "ok": True,
            "status": committed.status,
            "evidence_id": committed.event.evidence_id,
            "parent_evidence_id": parent.evidence_id,
            "feedback_value": committed.event.feedback_value,
            "decision_id": committed.event.decision_id,
            "observed_at": committed.event.observed_at,
        }

    @staticmethod
    def _empty_evidence_group(event: EvidenceEvent) -> dict[str, Any]:
        return {
            "profile_id": event.profile_id,
            "runtime_id": event.runtime_id,
            "reasoning_effort": event.reasoning_effort,
            "is_initial_routing_task": event.is_initial_routing_task,
            "context_bucket": (
                None
                if event.context_bucket is None
                else event.context_bucket.model_dump(mode="json")
            ),
            "outcomes": {
                name: 0
                for name in (
                    "verified",
                    "completed_unverified",
                    "partial",
                    "blocked",
                    "failed",
                    "interrupted",
                    "unresolved",
                    "cancelled",
                )

            },
            "normalized_value_counts": {"1.0": 0, "unknown": 0},
            "feedback": {
                name: 0
                for name in (
                    "rating-1",
                    "rating-2",
                    "rating-3",
                    "rating-4",
                    "rating-5",
                    "rejected",
                    "corrected",
                    "manual-reroute",
                )
            },
            "operations": {
                "api_calls": 0,
                "tool_iterations": 0,
                "retry_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cost_usd": 0.0,
            },
        }

    @staticmethod
    def _report_group_sort_key(key: tuple[Any, ...]) -> tuple[str, ...]:
        return tuple("" if value is None else str(value) for value in key)

    @staticmethod
    def _evidence_report_warnings(
        events: tuple[EvidenceEvent, ...],
    ) -> list[str]:
        warnings = ["descriptive_stage3_only", "non_active_routes_excluded"]
        turn_events = [
            event for event in events if event.source == "hermes_turn_outcome"
        ]
        if any(not event.is_initial_routing_task for event in turn_events):
            warnings.append("continuation_context_unavailable")
        if turn_events and all(
            event.latency_seconds is None for event in turn_events
        ):
            warnings.append("latency_unavailable")
        if any(event.normalized_value is None for event in turn_events):
            warnings.append("quality_unknown_present")
        feedback_by_parent: dict[str, set[str]] = {}
        for event in events:
            if event.source == "user_feedback" and event.parent_evidence_id:
                feedback_by_parent.setdefault(
                    event.parent_evidence_id,
                    set(),
                ).add(event.feedback_value or "")
        if any(len(values) > 1 for values in feedback_by_parent.values()):
            warnings.append("contradictory_feedback_present")
        return warnings

    def report(
        self,
        *,
        days: int = 30,
        decision_id: str | None = None,
        profile_id: str | None = None,
        runtime_id: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate immutable evidence events without changing routing state."""
        self._assert_profile_isolation()
        if (
            isinstance(days, bool)
            or not isinstance(days, int)
            or not 1 <= days <= 3650
        ):
            raise AutoRoutingServiceError(
                "report days must be between 1 and 3650"
            )
        now = _parse_timestamp(self._runtime_timestamp())
        since = (now - timedelta(days=days)).isoformat().replace("+00:00", "Z")
        events = self.store.list_evidence_events(
            decision_id=decision_id,
            profile_id=profile_id,
            runtime_id=runtime_id,
            reasoning_effort=reasoning_effort,
            observed_at_or_after=since,
        )
        turn_events = [
            event for event in events if event.source == "hermes_turn_outcome"
        ]
        feedback_events = [
            event for event in events if event.source == "user_feedback"
        ]
        groups: dict[tuple[Any, ...], dict[str, Any]] = {}
        for event in events:
            bucket_id = (
                event.context_bucket.bucket_id
                if event.context_bucket is not None
                else None
            )
            key = (
                event.profile_id,
                event.runtime_id,
                event.reasoning_effort,
                event.is_initial_routing_task,
                bucket_id,
            )
            group = groups.setdefault(key, self._empty_evidence_group(event))
            if event.source == "hermes_turn_outcome":
                assert event.outcome is not None
                group["outcomes"][event.outcome] += 1
                label = (
                    "unknown"
                    if event.normalized_value is None
                    else str(event.normalized_value)
                )
                group["normalized_value_counts"][label] += 1
                group["operations"]["api_calls"] += event.api_calls
                group["operations"]["tool_iterations"] += event.tool_iterations
                group["operations"]["retry_count"] += event.retry_count
                group["operations"]["input_tokens"] += event.input_tokens
                group["operations"]["output_tokens"] += event.output_tokens
                group["operations"]["cache_read_tokens"] += (
                    event.cache_read_tokens
                )
                group["operations"]["cost_usd"] += event.cost_usd
            else:
                assert event.feedback_value is not None
                group["feedback"][event.feedback_value] += 1
        return {
            "ok": True,
            "descriptive_only": True,
            "window": {
                "days": days,
                "observed_at_or_after": since,
            },
            "filters": {
                "decision_id": decision_id,
                "profile_id": profile_id,
                "runtime_id": runtime_id,
                "reasoning_effort": reasoning_effort,
            },
            "observations": {
                "turn_events": len(turn_events),
                "feedback_events": len(feedback_events),
                "quality_unknown_events": sum(
                    event.normalized_value is None for event in turn_events
                ),
                "initial_routing_task_events": sum(
                    event.is_initial_routing_task for event in turn_events
                ),
                "continuation_events": sum(
                    not event.is_initial_routing_task for event in turn_events
                ),
                "latency_observed_events": sum(
                    event.latency_seconds is not None for event in turn_events
                ),
            },
            "groups": [
                groups[key]
                for key in sorted(groups, key=self._report_group_sort_key)
            ],
            "warnings": self._evidence_report_warnings(events),
        }

    @staticmethod
    def _runtime_payload(runtime: Any) -> dict[str, Any]:
        return {
            "runtime_id": runtime.key.stable_id(),
            "key": runtime.key.model_dump(mode="json"),
            "provider": runtime.key.provider,
            "model": runtime.key.model,
            "state": runtime.state,
            "reasons": list(runtime.reasons),
            "economics": runtime.economics.model_dump(mode="json"),
            "reasoning": {
                "supported_efforts": list(runtime.reasoning_support.efforts),
                "source": runtime.reasoning_support.provenance,
            },
            "verification_source": runtime.verification_source,
            "verified_at": runtime.verified_at,
            "verification_expires_at": runtime.verification_expires_at,
            "provenance": list(runtime.provenance),
            "observed_at": runtime.observed_at,
        }

    def _new_inventory_service(
        self,
        *,
        policy: PolicyEnvelope | None = None,
    ) -> InventoryService:
        if policy is None and self.config_path.exists():
            try:
                policy = self._configured_authority().policy
            except AutoRoutingServiceError:
                policy = None
        return InventoryService(
            self.adapter,
            store=self.store,
            policy=policy,
        )

    def inventory(
        self,
        *,
        refresh: bool,
        include_ineligible: bool,
    ) -> dict[str, Any]:
        self._assert_profile_isolation()
        inventory_service = self._new_inventory_service()
        snapshot = inventory_service.refresh(refresh=refresh, persist=refresh)
        runtimes = tuple(
            runtime
            for runtime in snapshot.runtimes
            if include_ineligible or runtime.state != "ineligible"
        )
        return {
            "revision": snapshot.revision,
            "observed_at": snapshot.observed_at,
            "refreshed": refresh,
            "runtimes": [self._runtime_payload(runtime) for runtime in runtimes],
        }

    def verify_runtime(
        self,
        runtime_id: str,
        *,
        apply: bool,
        precondition_hash: str | None,
        acknowledge_billable: bool,
    ) -> dict[str, Any]:
        self._assert_profile_isolation()
        authority = self._configured_authority()
        inventory_service = InventoryService(
            self.adapter,
            store=self.store,
            policy=authority.policy,
        )
        if apply and precondition_hash is not None:
            stored_preview = self.store.read_verification_preview(
                precondition_hash
            )
            if (
                stored_preview is None
                or stored_preview.document.get("runtime_id") != runtime_id
            ):
                raise AutoRoutingServiceError(
                    "verification preview does not match the requested runtime"
                )
            inventory_service.restore_verification_preview(precondition_hash)
        else:
            inventory_service.refresh(refresh=False, persist=False)
        if not apply:
            preview = inventory_service.preview_verification(runtime_id)
            runtime = inventory_service._runtime_by_id(runtime_id)
            return {
                "applied": False,
                "billable": True,
                "runtime_id": runtime_id,
                "runtime": {
                    "provider": runtime.key.provider,
                    "model": runtime.key.model,
                    "api_mode": runtime.key.api_mode,
                    "auth_identity": runtime.key.auth_identity,
                    "endpoint_identity": runtime.key.endpoint_identity,
                },
                "billing_kind": runtime.economics.billing_kind,
                "economics_source": runtime.economics.source_id,
                "maximum_cost_usd": preview.worst_case_cost_usd,
                "maximum_quota_unit": preview.quota_unit,
                "maximum_quota_units": 1 if preview.quota_unit else None,
                "budget_reservation_class": "runtime-access-verification",
                "budget_day": preview.budget_day,
                "budget_ledger_revision": preview.budget_ledger_revision,
                "verification_ttl_seconds": 300,
                "policy_allow_paid_access_probes": (
                    authority.policy.allow_paid_access_probes
                ),
                "fixed_probe": {
                    "executor_id": preview.executor_id,
                    "executor_version": preview.executor_version,
                    "execution_shape_fingerprint": (
                        preview.execution_shape_fingerprint
                    ),
                    "maximum_input_tokens": preview.maximum_input_tokens,
                    "protocol_overhead_tokens": (
                        preview.protocol_overhead_tokens
                    ),
                    "maximum_output_tokens": preview.maximum_output_tokens,
                    "temperature": 0,
                    "tools": [],
                    "persist": False,
                },
                "precondition_hash": preview.precondition_hash,
                "expires_at": preview.expires_at,
            }
        if precondition_hash is None:
            raise AutoRoutingServiceError("verification precondition hash is required")
        runtime = inventory_service.apply_verification(
            precondition_hash,
            acknowledge_billable=acknowledge_billable,
        )
        return {
            "applied": True,
            "runtime_id": runtime.key.stable_id(),
            "state": runtime.state,
            "verification_source": runtime.verification_source,
            "verified_at": runtime.verified_at,
            "verification_expires_at": runtime.verification_expires_at,
        }

    def refresh_catalog(
        self,
        *,
        models_dev: bool,
        hermes: bool,
        files: list[str],
    ) -> dict[str, Any]:
        self._assert_profile_isolation()
        sources: list[Any] = [
            JsonCatalogSource(Path(path).read_bytes()) for path in files
        ]
        if models_dev or hermes:
            runtimes = self._new_inventory_service().refresh(
                refresh=False,
                persist=False,
            ).runtimes
            if models_dev:
                from .catalog import ModelsDevCatalogSource

                sources.append(ModelsDevCatalogSource(runtimes))
            if hermes:
                from hermes_cli.inventory import (
                    build_models_payload,
                    load_picker_context,
                )

                from .catalog import HermesCatalogSource

                sources.append(
                    HermesCatalogSource(
                        runtimes,
                        load_payload=lambda: build_models_payload(
                            load_picker_context(),
                            picker_hints=True,
                            pricing=True,
                            capabilities=True,
                            discovery_provenance=True,
                        ),
                    )
                )
        if not sources:
            raise AutoRoutingServiceError(
                "select --models-dev, --hermes, or at least one --file"
            )
        catalog = CatalogService(store=self.store)
        snapshot = catalog.refresh(sources)
        return {
            "snapshot_id": snapshot.snapshot_id,
            "record_count": len(snapshot.evidence),
            "created_at": snapshot.created_at,
            "stale_fallback": snapshot.stale_fallback,
            "source_errors": list(snapshot.source_errors),
        }

    def rank_profiles(self, request_data: Mapping[str, Any]) -> dict[str, Any]:
        """Rank every authored profile without selecting or persisting targets."""
        self._assert_profile_isolation()
        request = AdvisorRequest.ranking_request(request_data)
        profile_rankings, _proposals, _inventory, _catalog = (
            self._rank_profile_requests(request)
        )
        return {"profile_rankings": profile_rankings}

    def _rank_profile_requests(
        self,
        request: AdvisorRankingRequest,
        *,
        maximum_reasoning: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], Any, CatalogService]:
        declared_reasoning = tuple(
            profile.limits.max_reasoning_effort
            for profile in request.profiles
            if profile.limits is not None
            and profile.limits.max_reasoning_effort is not None
        )
        if maximum_reasoning is None:
            maximum_reasoning = (
                max(declared_reasoning, key=REASONING_EFFORT_ORDER.index)
                if declared_reasoning
                else REASONING_EFFORT_ORDER[-1]
            )
        policy = self._advisor_policy(request, maximum_reasoning)
        inventory_service = self._new_inventory_service(policy=policy)
        inventory = inventory_service.refresh(refresh=False, persist=False)
        inventory = inventory.__class__(
            revision=inventory.revision,
            runtimes=[
                self._apply_request_hard_limits(runtime, request)
                for runtime in inventory.runtimes
            ],
            observed_at=inventory.observed_at,
        )
        catalog = CatalogService(store=self.store)
        advisor = Advisor(catalog)
        profile_rankings: dict[str, Any] = {}
        proposals: dict[str, Any] = {}
        for profile in request.profiles:
            profile_inventory = tuple(
                self._apply_profile_hard_limits(runtime, profile)
                for runtime in inventory.runtimes
            )
            required_capabilities = tuple(
                dict.fromkeys(
                    (
                        *request.required_capabilities,
                        *(("tools",) if request.risk_and_tool_use.requires_tools else ()),
                        *profile.match.capabilities,
                    )
                )
            )
            required_modalities = tuple(
                dict.fromkeys((*request.modalities, *profile.match.modalities))
            )
            limits = profile.limits
            minimum_context_tokens = (
                request.workloads.expected_input_tokens
                + request.workloads.expected_output_tokens
            )
            if limits is not None and limits.minimum_context_tokens is not None:
                minimum_context_tokens = max(
                    minimum_context_tokens,
                    limits.minimum_context_tokens,
                )
            evidence_domains = tuple(
                sorted(set(profile.match.domains or request.workloads.domains))
            )
            proposal = advisor.propose(
                ProposalRequest(
                    inventory=profile_inventory,
                    domain=evidence_domains[0],
                    evidence_domains=evidence_domains,
                    task_definition=request.workloads.examples[0],
                    expected_input_tokens=request.workloads.expected_input_tokens,
                    expected_output_tokens=request.workloads.expected_output_tokens,
                    required_capabilities=required_capabilities,
                    required_modalities=required_modalities,
                    minimum_context_tokens=minimum_context_tokens,
                    minimum_output_tokens=request.workloads.expected_output_tokens,
                    objectives=profile.objectives,
                    max_estimated_task_cost_usd=(
                        limits.max_estimated_task_cost_usd
                        if limits is not None
                        and limits.max_estimated_task_cost_usd is not None
                        else request.hard_limits.max_cost_usd
                    ),
                    max_estimated_latency_seconds=(
                        limits.max_estimated_latency_seconds
                        if limits is not None
                        and limits.max_estimated_latency_seconds is not None
                        else request.hard_limits.max_latency_seconds
                    ),
                    # Profile base rank is a profile-selection prior. It must
                    # never bias runtime ranking inside that profile.
                    base_ranks={},
                )
            )
            proposals[profile.profile_id] = proposal
            candidates = []
            for runtime_id in proposal.explanation.accepted_runtime_ids:
                detail = self._json_value(
                    proposal.explanation.candidates[runtime_id]
                )
                sources = list(detail.get("sources") or ())
                economics_source = detail.get("economics_source") or {}
                provenance = [
                    {
                        "source_id": source.get("source_id"),
                        "source_url": source.get("source_url"),
                    }
                    for source in sources
                ]
                if economics_source:
                    provenance.append({
                        "source_id": economics_source.get("source_id"),
                        "provenance": economics_source.get("provenance"),
                    })
                source_dates = [
                    {
                        "retrieved_at": source.get("retrieved_at"),
                        "published_at": source.get("published_at"),
                        "expires_at": source.get("expires_at"),
                    }
                    for source in sources
                ]
                if economics_source.get("observed_at"):
                    source_dates.append({
                        "observed_at": economics_source["observed_at"]
                    })
                confidences = [
                    float(confidence)
                    for confidence in (
                        *(source.get("confidence") for source in sources),
                        economics_source.get("confidence"),
                    )
                    if confidence is not None
                ]
                candidates.append({
                    **detail,
                    "provenance": provenance,
                    "source_dates": source_dates,
                    "confidence": min(confidences) if confidences else 0.0,
                    "uncertainty": detail.get(
                        "uncertainty_components",
                        {},
                    ),
                })
            profile_rankings[profile.profile_id] = {
                "runtime_ids": list(proposal.explanation.accepted_runtime_ids),
                "candidates": candidates,
                "rejected_candidates": self._json_value(
                    proposal.explanation.rejected_candidates
                ),
            }
        return profile_rankings, proposals, inventory, catalog

    @staticmethod
    def _apply_profile_hard_limits(runtime: Any, profile: Any) -> Any:
        limits = profile.limits
        if limits is None or runtime.state != "verified":
            return runtime
        additions: list[str] = []
        if limits.allowed_licenses is not None and runtime.key.local_backend:
            if runtime.capabilities.get("license_id") not in limits.allowed_licenses:
                additions.append("license_not_allowed")
        if limits.max_reasoning_effort is not None:
            maximum_index = REASONING_EFFORT_ORDER.index(
                limits.max_reasoning_effort
            )
            if not any(
                REASONING_EFFORT_ORDER.index(effort) <= maximum_index
                for effort in runtime.reasoning_support.efforts
            ):
                additions.append("reasoning_out_of_bounds")
        if not additions:
            return runtime
        reasons = list(runtime.reasons)
        reasons.extend(reason for reason in additions if reason not in reasons)
        return replace(
            runtime,
            state="ineligible",
            reasons=type(runtime.reasons)(reasons),
            verification_source=None,
            verified_at=None,
            verification_expires_at=None,
        )

    def plan(
        self,
        request_path: str | Path,
        *,
        prompt_files: list[str],
    ) -> dict[str, Any]:
        self._assert_profile_isolation()
        try:
            raw = fast_safe_load(Path(request_path).read_bytes())
        except Exception as error:
            raise AutoRoutingServiceError("advisor request could not be read") from error
        if not isinstance(raw, Mapping):
            raise AutoRoutingServiceError("advisor request must contain a mapping")
        request_data = dict(raw)
        if prompt_files:
            prompts = list(request_data.get("representative_prompts") or ())
            for prompt_path in prompt_files:
                prompts.append(Path(prompt_path).read_text(encoding="utf-8"))
            request_data["representative_prompts"] = prompts
        readiness = AdvisorRequest.validate_readiness(request_data)
        ranking_missing = tuple(
            item
            for item in readiness.missing_facts
            if item not in {"representative_prompts", "explicit_approval"}
            and not item.endswith(".access_paths")
            and not item.endswith(".reasoning_bounds")
        )
        profile_rankings: dict[str, Any] = {}
        if not readiness.ready and not ranking_missing:
            profile_rankings = self.rank_profiles(request_data)[
                "profile_rankings"
            ]
        if not readiness.ready:
            return {
                "ready": False,
                "missing_facts": list(readiness.missing_facts),
                "applied": False,
                "profile_rankings": profile_rankings,
                "next_command": None,
            }
        request = AdvisorRequest.model_validate(request_data)
        return self._complete_plan(request)

    def _complete_plan(self, request: AdvisorRequest) -> dict[str, Any]:
        reasoning_values = [
            bounds.maximum
            for profile in request.profiles
            for bounds in profile.reasoning_bounds.values()
        ]
        reasoning_values.extend(
            profile.limits.max_reasoning_effort
            for profile in request.profiles
            if profile.limits is not None
            and profile.limits.max_reasoning_effort is not None
        )
        maximum_reasoning = max(
            reasoning_values,
            key=REASONING_EFFORT_ORDER.index,
        )
        ranking_request = AdvisorRequest.ranking_request(
            request.model_dump(mode="json", by_alias=True)
        )
        (
            computed_rankings,
            ranked_proposals,
            inventory,
            catalog,
        ) = self._rank_profile_requests(
            ranking_request,
            maximum_reasoning=maximum_reasoning,
        )
        profile_rankings = computed_rankings
        policy = self._advisor_policy(request, maximum_reasoning)
        runtime_by_id = {
            runtime.key.stable_id(): runtime for runtime in inventory.runtimes
        }

        profiles: dict[str, RouteProfile] = {}
        targets_output: list[dict[str, Any]] = []
        resolver_validation: list[dict[str, Any]] = []
        selected_runtimes: dict[str, Any] = {}
        for profile in request.profiles:
            target_ids = (
                profile.access_paths.primary_runtime_id,
                *profile.access_paths.fallback_runtime_ids,
            )
            accepted_runtime_ids = set(
                profile_rankings[profile.profile_id]["runtime_ids"]
            )
            rejected_requested = {
                runtime_id: profile_rankings[profile.profile_id][
                    "rejected_candidates"
                ].get(runtime_id, {"reasons": ["not_ranked"]})
                for runtime_id in target_ids
                if runtime_id not in accepted_runtime_ids
            }
            if rejected_requested:
                raise AutoRoutingServiceError(
                    f"profile {profile.profile_id} requested runtimes failed "
                    "advisor hard gates: "
                    + _canonical_json(rejected_requested)
                )

            limits = profile.limits
            effective_cost_limit = (
                limits.max_estimated_task_cost_usd
                if limits is not None
                and limits.max_estimated_task_cost_usd is not None
                else request.hard_limits.max_cost_usd
            )
            effective_latency_limit = (
                limits.max_estimated_latency_seconds
                if limits is not None
                and limits.max_estimated_latency_seconds is not None
                else request.hard_limits.max_latency_seconds
            )
            profile_targets: list[RoutingTarget] = []
            profile_runtimes: list[Any] = []
            for index, runtime_id in enumerate(target_ids):
                runtime = runtime_by_id.get(runtime_id)
                if runtime is None:
                    raise AutoRoutingServiceError(
                        "requested runtime is absent from executable inventory: "
                        f"{runtime_id}"
                    )
                if runtime.state != "verified":
                    reasons = ", ".join(runtime.reasons) or "not_verified"
                    raise AutoRoutingServiceError(
                        "requested runtime is not verified executable "
                        f"({reasons}): {runtime_id}"
                    )
                resolved = self.adapter.resolve(runtime.key)
                if resolved.runtime_key != runtime.key:
                    raise AutoRoutingServiceError(
                        f"resolver identity changed for runtime: {runtime_id}"
                    )
                reasoning = profile.reasoning_bounds[runtime_id]
                supported_efforts = set(runtime.reasoning_support.efforts)
                if not {
                    reasoning.minimum,
                    reasoning.default,
                    reasoning.maximum,
                }.issubset(supported_efforts):
                    raise AutoRoutingServiceError(
                        "requested reasoning bounds are not executable: "
                        f"{runtime_id}"
                    )
                target = RoutingTarget(
                    runtime=runtime.key,
                    reasoning=reasoning,
                    supported_reasoning_efforts=tuple(
                        runtime.reasoning_support.efforts
                    ),
                    max_estimated_task_cost_usd=effective_cost_limit,
                    max_estimated_latency_seconds=effective_latency_limit,
                    revision_status="active" if index == 0 else "fallback",
                )
                profile_targets.append(target)
                profile_runtimes.append(runtime)
                selected_runtimes[runtime_id] = runtime
                resolver_validation.append({
                    "profile_id": profile.profile_id,
                    "runtime_id": runtime_id,
                    "resolver_name": resolved.resolver_name,
                    "provider": resolved.provider,
                    "api_mode": resolved.api_mode,
                    "exact_match": True,
                })
                targets_output.append({
                    "profile_id": profile.profile_id,
                    "runtime_id": runtime_id,
                    "provider": runtime.key.provider,
                    "model": runtime.key.model,
                    "resolution_status": runtime.state,
                    "reasoning": reasoning.model_dump(
                        mode="json",
                        by_alias=True,
                    ),
                    "supported_reasoning_efforts": list(
                        runtime.reasoning_support.efforts
                    ),
                    "sources": [
                        row.model_dump(mode="json")
                        for row in catalog.evidence_for(runtime)
                    ],
                })
            evidence_urls = tuple(
                sorted({
                    row.source_url
                    for runtime in profile_runtimes
                    for row in catalog.evidence_for(runtime)
                })
            ) or ("inventory:verified-executable",)
            profiles[profile.profile_id] = RouteProfile(
                profile_id=profile.profile_id,
                description=profile.description,
                base_rank=profile.base_rank,
                match=profile.match,
                objectives=profile.objectives,
                limits=profile.limits,
                primary=profile_targets[0],
                fallbacks=tuple(profile_targets[1:]),
                provenance=evidence_urls,
            )

        disclosure = request.classifier_evaluator_disclosure
        proposal = AutoRoutingConfig(
            llm=PluginLlmAuthority(
                allow_provider_override=True,
                allowed_providers=tuple(
                    dict.fromkeys((
                        disclosure.classifier_provider,
                        disclosure.evaluator_provider,
                    ))
                ),
                allow_model_override=True,
                allowed_models=tuple(
                    dict.fromkeys((
                        disclosure.classifier_model,
                        disclosure.evaluator_model,
                    ))
                ),
            ),
            activation=ActivationSettings(mode="shadow"),
            scopes=RoutingScopes(fresh_sessions=True, delegation=True),
            classifier=ClassifierSettings(
                provider=disclosure.classifier_provider,
                model=disclosure.classifier_model,
                reasoning_effort="low",
                timeout_seconds=30,
                disclosure="full",
            ),
            safe_default="inherit",
            policy=policy,
            adaptation=AdaptationSettings(
                enabled=False,
                mode="autonomous",
                canary_fraction=0,
                minimum_canary_samples=0,
                rollback_threshold=0,
            ),
            rules=request.rules,
            complexity_bands=request.complexity_bands,
            routing_vocabulary=request.routing_vocabulary,
            profiles=profiles,
            economics_overrides={},
        )
        config_preview = preview_update(proposal, path=self.config_path)
        authority_id = authority_revision(proposal)
        baseline = self._baseline_revision(proposal, authority_id=authority_id)
        baseline_document = baseline.model_dump(mode="json", by_alias=True)
        baseline_json = _canonical_json(baseline_document)

        first_profile_id = request.profiles[0].profile_id
        advisor = Advisor(catalog)
        dry_run = advisor.dry_run(
            list(request.representative_prompts),
            ranked_proposals[first_profile_id],
        )
        ranking = profile_rankings[first_profile_id]["candidates"]
        return {
            "ready": True,
            "readiness": {"ready": True, "missing_facts": []},
            "applied": False,
            "targets": targets_output,
            "ranking": ranking,
            "profile_rankings": profile_rankings,
            "rejected_candidates": profile_rankings[first_profile_id][
                "rejected_candidates"
            ],
            "economics_by_access_path": {
                runtime.key.stable_id(): runtime.economics.model_dump(mode="json")
                for runtime in selected_runtimes.values()
            },
            "dry_run": {
                "proposed_runtime_ids": list(dry_run.proposed_runtime_ids),
                "results": [asdict(item) for item in dry_run.assessments],
            },
            "resolver_validation": resolver_validation,
            "yaml_diff": config_preview.unified_diff,
            "proposal": self._config_document(proposal),
            "authority_id": authority_id,
            "initial_revision": {
                "document": baseline_document,
                "canonical_json": baseline_json,
                "checksum": _checksum(baseline_json),
            },
            "next_command": (
                "hermes auto-routing setup --proposal <reviewed-proposal-file>"
            ),
        }

    @staticmethod
    def _advisor_policy(
        request: AdvisorRequest,
        maximum_reasoning: Any,
    ) -> PolicyEnvelope:
        return PolicyEnvelope(
            eligible_sources=(
                "configured_providers",
                *(
                    ("installed_local_models",)
                    if request.hard_limits.allow_local
                    else ()
                ),
            ),
            uninstalled_local_models="deny",
            local_models=LocalModelRequirements(
                require_open_weights=True,
                require_compatible_hardware=True,
            ),
            denied_providers=request.hard_limits.denied_providers,
            denied_models=request.hard_limits.denied_models,
            max_estimated_task_cost_usd=request.hard_limits.max_cost_usd,
            max_estimated_latency_seconds=request.hard_limits.max_latency_seconds,
            max_routing_overhead_usd_per_day=0,
            max_experiment_cost_usd_per_day=0,
            max_evaluator_calls_per_day=0,
            max_canary_fraction=0,
            max_reasoning_effort=maximum_reasoning,
            allow_subscription=request.hard_limits.allow_subscription,
            allow_paid_access_probes=False,
            allowed_licenses=request.hard_limits.allowed_licenses,
            minimum_context_tokens=0,
            canary_high_risk_tasks=False,
        )

    @staticmethod
    def _apply_request_hard_limits(runtime: Any, request: AdvisorRequest) -> Any:
        reasons = list(runtime.reasons)
        hard_limits = request.hard_limits
        additions: list[str] = []
        if runtime.key.provider in hard_limits.denied_providers:
            additions.append("provider_denied_by_request")
        if runtime.key.model in hard_limits.denied_models:
            additions.append("model_denied_by_request")
        if runtime.key.local_backend and not hard_limits.allow_local:
            additions.append("local_runtime_denied_by_request")
        if (
            runtime.economics.billing_kind == "subscription"
            and not hard_limits.allow_subscription
        ):
            additions.append("subscription_denied_by_request")
        for reason in additions:
            if reason not in reasons:
                reasons.append(reason)
        if not additions:
            return runtime
        return replace(
            runtime,
            state="ineligible",
            reasons=type(runtime.reasons)(reasons),
            verification_source=None,
            verified_at=None,
            verification_expires_at=None,
        )

    @classmethod
    def _json_value(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): cls._json_value(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [cls._json_value(item) for item in value]
        return value

    @staticmethod
    def _explain_identifier(value: str, *, name: str) -> str:
        if (
            not isinstance(value, str)
            or _EXPLAIN_IDENTIFIER.fullmatch(value) is None
        ):
            raise ValueError(f"{name} must be a bounded content-free identifier")
        return value

    def explain(
        self,
        *,
        decision_id: str | None = None,
        session_id: str | None = None,
        operation_id: str | None = None,
        task_index: int | None = None,
        detailed: bool = False,
    ) -> dict[str, Any]:
        """Return one immutable decision explanation without live execution."""
        self._assert_profile_isolation()
        selectors = tuple(
            name
            for name, value in (
                ("decision", decision_id),
                ("session", session_id),
                ("operation", operation_id),
            )
            if value is not None
        )
        if len(selectors) != 1:
            raise ValueError(
                "explain requires exactly one decision, session, or operation lookup"
            )
        if operation_id is None and task_index is not None:
            raise ValueError("task index requires an operation lookup")
        if operation_id is not None and (
            isinstance(task_index, bool)
            or not isinstance(task_index, int)
            or not 0 <= task_index <= MAX_TASK_INDEX
        ):
            raise ValueError("operation lookup requires a bounded task index")

        lookup: dict[str, Any]
        if decision_id is not None:
            identifier = self._explain_identifier(
                decision_id,
                name="decision id",
            )
            decision = self.store.read_decision(identifier)
            lookup = {"kind": "decision", "decision_id": identifier}
        elif session_id is not None:
            identifier = self._explain_identifier(
                session_id,
                name="session id",
            )
            decision = self.store.read_session_decision(identifier)
            lookup = {"kind": "session", "session_id": identifier}
        else:
            assert operation_id is not None and task_index is not None
            identifier = self._explain_identifier(
                operation_id,
                name="operation id",
            )
            decision = self.store.read_operation_decision(identifier, task_index)
            lookup = {
                "kind": "operation",
                "operation_id": identifier,
                "task_index": task_index,
            }
        if decision is None:
            raise AutoRoutingServiceError("routing decision not found")
        evidence = self.store.list_evidence_events(
            decision_id=decision.decision_id
        )
        candidates = (
            self.store.read_decision_candidates(decision.decision_id)
            if detailed
            else ()
        )
        result = {
            **serialize_decision_explanation(
                decision,
                candidates=candidates,
                detailed=detailed,
            ),
            "lookup": lookup,
            "evidence": {
                "event_ids": [event.evidence_id for event in evidence],
                "turn_outcomes": sum(
                    event.source == "hermes_turn_outcome" for event in evidence
                ),
                "explicit_feedback": sum(
                    event.source == "user_feedback" for event in evidence
                ),
                "quality_unknown": sum(
                    event.source == "hermes_turn_outcome"
                    and event.normalized_value is None
                    for event in evidence
                ),
            },
        }
        if detailed:
            result["evidence_events"] = [
                event.model_dump(mode="json") for event in evidence
            ]
        return result

    def status(self) -> dict[str, Any]:
        """Report effective activation without constructing or routing an agent."""
        self._assert_profile_isolation()
        activation_mode = "off"
        configured_activation_mode = "off"
        authority_id = None
        activation_receipt_id = None
        projection_reason = "authority_unavailable"
        incomplete_config_apply = self._has_incomplete_config_apply()
        if self.config_path.exists() and not incomplete_config_apply:
            try:
                proposal = parse_config(fast_safe_load(self.config_path.read_bytes()))
            except Exception:
                pass
            else:
                authority_id = authority_revision(proposal)
                configured_activation_mode = proposal.activation.mode
                if self._authority_is_usable(proposal, authority_id):
                    if proposal.activation.mode != "active":
                        activation_mode = proposal.activation.mode
                        projection_reason = f"routing_{proposal.activation.mode}"
                    else:
                        try:
                            _report, adapter_sha = self._adapter_contract(self.adapter)
                            receipt = self.store.read_matching_activation_receipt(
                                authority_id=authority_id,
                                config_sha=config_revision(proposal),
                                adapter_capability_sha=adapter_sha,
                            )
                        except Exception:
                            receipt = None
                        if receipt is not None:
                            activation_mode = "active"
                            activation_receipt_id = receipt.receipt_id
                            projection_reason = "active_receipt_matched"
                        else:
                            projection_reason = "activation_receipt_missing"
        try:
            self._adapter_contract(self.adapter)
            runtime_projection = "available"
        except Exception:
            runtime_projection = "not_installed"
        return {
            "activation_mode": activation_mode,
            "configured_activation_mode": configured_activation_mode,
            "authority_id": authority_id,
            "activation_receipt_id": activation_receipt_id,
            "projection_reason": projection_reason,
            "runtime_projection": runtime_projection,
            "routing_decision_count": self.store.count_decisions(),
            "incomplete_config_apply": incomplete_config_apply,
            "command": "status",
            "write_class": "read_only",
        }

    def _authority_is_usable(
        self,
        proposal: AutoRoutingConfig,
        authority_id: str,
    ) -> bool:
        """Compatibility name for baseline activation integrity."""
        return self._baseline_activation_is_usable(proposal, authority_id)

    def _baseline_activation_is_usable(
        self,
        proposal: AutoRoutingConfig,
        authority_id: str,
    ) -> bool:
        """Validate the legacy activation baseline independently of overlays."""
        stored = self.store.read_authority_revision(authority_id)
        baseline = self.store.read_active_revision(authority_id)
        if stored is None or baseline is None or not baseline.is_baseline:
            return False
        authority_json = _canonical_json(self._authority_document(proposal))
        expected_baseline = self._baseline_revision(
            proposal,
            authority_id=authority_id,
        )
        return (
            stored.document_json == authority_json
            and stored.checksum == _checksum(authority_json)
            and baseline == expected_baseline
        )

    def _active_profile_overlay_is_usable(
        self,
        proposal: AutoRoutingConfig,
        authority_id: str,
        profile_id: str,
    ) -> bool:
        """Validate one current typed overlay without requiring baseline identity."""
        try:
            revision, _generation = self.store.read_active_profile_revision(
                authority_id,
                profile_id,
            )
            if revision is None:
                return True
            return bool(
                revision.complete
                and revision.authority_id == authority_id
                and revision.profile_id == profile_id
                and validate_overlay(proposal, revision.overlay) == revision.overlay
            )
        except Exception:
            return False

    def doctor(
        self,
        *,
        _proposal: AutoRoutingConfig | None = None,
        _activation_transition: bool = False,
    ) -> dict[str, Any]:
        """Read-only validation of authority and the Stage 2 projection seam."""
        checks: list[dict[str, Any]] = []

        def check(name: str, status: str, detail: Any = None) -> None:
            checks.append({"name": name, "status": status, "detail": detail})

        try:
            self._assert_profile_isolation()
        except AutoRoutingServiceError as error:
            check("profile_isolation", "error", str(error))
            check(
                "runtime_adapter",
                "not_installed",
                "Stage 2 adapter is intentionally absent",
            )
            return {
                "healthy": False,
                "incomplete_config_apply": self._incomplete_config_apply,
                "checks": checks,
                "runtime_projection": "not_installed",
            }

        proposal: AutoRoutingConfig | None = _proposal
        authority_id: str | None = None
        if proposal is None:
            try:
                proposal = self._configured_authority()
            except Exception as error:
                check("config_schema", "error", str(error))
            else:
                authority_id = authority_revision(proposal)
                check("config_schema", "ok", "authority schema is valid")
        else:
            authority_id = authority_revision(proposal)
            check(
                "config_schema",
                "ok",
                "proposed activation authority schema is valid",
            )

        home = self.hermes_home.resolve()
        isolated = all(
            path.resolve().is_relative_to(home)
            for path in (self.config_path, self.store.path)
        )
        check(
            "profile_isolation",
            "ok" if isolated else "error",
            str(home),
        )

        try:
            integrity = str(
                self.store.connection.execute("PRAGMA integrity_check").fetchone()[0]
            )
            journal_mode = str(
                self.store.connection.execute("PRAGMA journal_mode").fetchone()[0]
            ).lower()
            schema = self.store.connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            db_ok = (
                integrity == "ok"
                and journal_mode in {"wal", "delete"}
                and schema is not None
            )
        except Exception as error:
            check("database", "error", str(error))
        else:
            check(
                "database",
                "ok" if db_ok else "error",
                {"integrity": integrity, "journal_mode": journal_mode},
            )

        pending = self._pending_apply_journals()
        pending_error = self._incomplete_config_apply or bool(pending)
        check(
            "pending_apply_journal",
            "error" if pending_error else "ok",
            self._recovery_error or [path.name for path in pending],
        )

        inventory_revision: str | None = None
        inventory_contract_sha: str | None = None
        inventory_snapshot: Any = None
        if proposal is not None:
            try:
                inventory_snapshot, inventory_contract_sha = (
                    self._activation_inventory_fingerprint(proposal)
                )
                inventory_revision = inventory_snapshot.snapshot_id
            except Exception as error:
                check(
                    "inventory_contract",
                    "error",
                    f"inventory_contract_unavailable:{type(error).__name__}",
                )
            else:
                check(
                    "inventory_contract",
                    "ok",
                    {
                        "inventory_revision": inventory_revision,
                        "inventory_contract_sha": inventory_contract_sha,
                    },
                )

        if proposal is not None and authority_id is not None:
            authority_detail: Any = authority_id
            if _activation_transition:
                try:
                    current_authority = self._configured_authority()
                    current_authority_id = authority_revision(current_authority)
                    authority_ok = self._authority_is_usable(
                        current_authority,
                        current_authority_id,
                    )
                    authority_detail = {
                        "current": current_authority_id,
                        "proposed": authority_id,
                    }
                except Exception:
                    authority_ok = False
            else:
                authority_ok = self._authority_is_usable(proposal, authority_id)
            check(
                "authority_baseline",
                "ok" if authority_ok else "error",
                authority_detail,
            )
            inherited_runtime_id: str | None = None
            if isinstance(proposal.safe_default, str):
                safe_status, safe_detail, inherited_runtime_id = (
                    self._doctor_inherited_safe_default(
                        proposal,
                        inventory_snapshot,
                    )
                )
                check("safe_default", safe_status, safe_detail)
            else:
                safe_status, safe_detail = self._doctor_explicit_safe_default(
                    proposal,
                    inventory_snapshot,
                )
                check("safe_default", safe_status, safe_detail)
            classifier_ok, classifier_detail = self._doctor_classifier_contract(
                proposal,
                inventory_snapshot,
            )
            check(
                "classifier_evaluator_trust",
                "ok" if classifier_ok else "error",
                classifier_detail,
            )
            target_ids = {
                target.runtime.stable_id()
                for profile in proposal.profiles.values()
                for target in (*profile.primary_choices(), *profile.fallbacks)
            }
            if not isinstance(proposal.safe_default, str):
                target_ids.add(proposal.safe_default.runtime.stable_id())
            elif inherited_runtime_id is not None:
                target_ids.add(inherited_runtime_id)
            now = datetime.now(UTC)
            target_states = {}
            current_observations = {
                observation.key.stable_id(): observation
                for observation in (
                    ()
                    if inventory_snapshot is None
                    else inventory_snapshot.observations
                )
            }
            for runtime_id in sorted(target_ids):
                observation = current_observations.get(runtime_id)
                expires_at = (
                    None
                    if observation is None
                    else observation.verification_expires_at
                )
                try:
                    unexpired = (
                        expires_at is not None
                        and _parse_timestamp(expires_at) > now
                    )
                except (TypeError, ValueError):
                    unexpired = False
                target_states[runtime_id] = {
                    "state": None if observation is None else observation.state,
                    "verification_expires_at": expires_at,
                    "unexpired": unexpired,
                }
            targets_ok = bool(target_states) and all(
                detail["state"] == "verified" and detail["unexpired"]
                for detail in target_states.values()
            )
            check(
                "runtime_verification",
                "ok" if targets_ok else "error",
                target_states,
            )

        inventory_row = self.store.connection.execute(
            "SELECT created_at FROM inventory_snapshots "
            "WHERE complete = 1 ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        inventory_created = (
            None if inventory_row is None else str(inventory_row["created_at"])
        )
        try:
            inventory_fresh = (
                inventory_created is not None
                and datetime.now(UTC) - _parse_timestamp(inventory_created)
                <= OBSERVATION_FRESHNESS
            )
        except (TypeError, ValueError):
            inventory_fresh = False
        check(
            "inventory_freshness",
            "ok" if inventory_fresh else "warning",
            inventory_created,
        )
        catalog_row = self.store.connection.execute(
            "SELECT created_at FROM catalog_snapshots "
            "WHERE complete = 1 ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        catalog_created = (
            None if catalog_row is None else str(catalog_row["created_at"])
        )
        try:
            catalog_fresh = (
                catalog_created is not None
                and datetime.now(UTC) - _parse_timestamp(catalog_created)
                <= OBSERVATION_FRESHNESS
            )
        except (TypeError, ValueError):
            catalog_fresh = False
        check(
            "catalog_freshness",
            "ok" if catalog_fresh else "warning",
            catalog_created,
        )
        adapter_report: dict[str, Any] | None = None
        adapter_sha: str | None = None
        adapter_error: str | None = None
        try:
            adapter_report, adapter_sha = self._adapter_contract(self.adapter)
        except Exception as error:
            adapter_error = str(error) or type(error).__name__
        stage2_required = bool(
            _activation_transition
            or (
                proposal is not None
                and proposal.activation.mode == "active"
            )
        )
        if adapter_report is None:
            check(
                "runtime_adapter",
                "error" if stage2_required else "not_installed",
                adapter_error,
            )
        else:
            check(
                "runtime_adapter",
                "ok",
                {
                    "contract": adapter_report["contract"],
                    "capability_sha": adapter_sha,
                },
            )
            resolver_ok, resolver_detail = self._doctor_resolver_contract()
            check(
                "resolver_registration",
                (
                    "ok"
                    if resolver_ok
                    else ("error" if stage2_required else "warning")
                ),
                resolver_detail,
            )
            for name in (
                "fresh_session",
                "delegation",
                "exact_credential_pool",
                "reasoning_projection",
                "pre_call_fallback",
            ):
                check(
                    name,
                    "ok" if adapter_report.get(name) is True else "error",
                    adapter_report.get(name),
                )
            check(
                "post_call_model_failover",
                "warning",
                "disabled",
            )

        if (
            proposal is not None
            and adapter_report is not None
            and inventory_snapshot is not None
        ):
            exact_ok, exact_detail = self._doctor_exact_projection_contracts(
                proposal,
                inventory_snapshot,
            )
            check(
                "exact_targets",
                "ok" if exact_ok else "error",
                exact_detail,
            )

        receipt_detail: Any = "not required"
        receipt_status = "ok"
        if proposal is not None and proposal.activation.mode == "active":
            if _activation_transition:
                receipt_detail = "will be written atomically with active config"
            elif adapter_sha is None:
                receipt_status = "error"
                receipt_detail = "adapter capability fingerprint is unavailable"
            else:
                receipt = self.store.read_matching_activation_receipt(
                    authority_id=authority_revision(proposal),
                    config_sha=config_revision(proposal),
                    adapter_capability_sha=adapter_sha,
                )
                if receipt is None:
                    receipt_status = "error"
                    receipt_detail = "matching activation receipt is missing"
                else:
                    receipt_detail = {
                        "receipt_id": receipt.receipt_id,
                        "authority_id": receipt.authority_id,
                        "config_sha": receipt.config_sha,
                        "inventory_contract_sha": (
                            receipt.inventory_contract_sha
                        ),
                        "inventory_revision": receipt.inventory_revision,
                        "adapter_capability_sha": (
                            receipt.adapter_capability_sha
                        ),
                    }
        check("activation_receipt", receipt_status, receipt_detail)
        healthy = not any(item["status"] == "error" for item in checks)
        return {
            "healthy": healthy,
            "incomplete_config_apply": pending_error,
            "checks": checks,
            "runtime_projection": (
                "available" if adapter_report is not None else "not_installed"
            ),
            "fingerprints": {
                "authority_id": authority_id,
                "config_sha": (
                    None if proposal is None else config_revision(proposal)
                ),
                "inventory_revision": inventory_revision,
                "inventory_contract_sha": inventory_contract_sha,
                "adapter_capability_sha": adapter_sha,
            },
        }

    def _doctor_resolver_contract(self) -> tuple[bool, dict[str, Any]]:
        manager = getattr(self.plugin_context, "_manager", None)
        resolver = (
            None if manager is None else manager.agent_runtime_resolver
        )
        owner = (
            None if manager is None else manager.agent_runtime_resolver_owner
        )
        detail: dict[str, Any] = {
            "owner": owner,
            "registered": resolver is not None,
        }
        if resolver is None or owner != "auto-routing":
            return False, {**detail, "reason": "resolver_not_registered"}
        expected_parameters = {
            "requires_initial_task": ("scope",),
            "resolve": ("request",),
            "record_manual_pin": ("request",),
            "record_session_continuation": ("request",),
        }
        for name, expected in expected_parameters.items():
            method = getattr(resolver, name, None)
            if not callable(method):
                return False, {
                    **detail,
                    "reason": f"resolver_method_missing:{name}",
                }
            parameters = tuple(inspect.signature(method).parameters)
            if parameters != expected:
                return False, {
                    **detail,
                    "reason": f"resolver_signature_invalid:{name}",
                }
        try:
            fresh = resolver.requires_initial_task("fresh_session")
            delegation = resolver.requires_initial_task("delegation")
        except Exception:
            return False, {**detail, "reason": "resolver_boundary_error"}
        if fresh is not True or delegation is not True:
            return False, {**detail, "reason": "resolver_boundary_invalid"}
        return True, {
            **detail,
            "fresh_session": True,
            "delegation": True,
        }

    def _persisted_projection_rejection_reasons(
        self,
        runtime: Any,
        *,
        reasoning_effort: str,
        root_config: Mapping[str, Any],
    ) -> tuple[str, ...]:
        """Inspect one persisted projection without invoking live resolution."""
        rejections: list[str] = []
        inspect_projection = getattr(
            self.adapter,
            "inspect_persisted_projection",
            None,
        )
        if not callable(inspect_projection):
            return ("persisted_projection_inspection_unavailable",)
        try:
            projection = inspect_projection(
                runtime,
                reasoning_effort=reasoning_effort,
                hermes_config=root_config,
            )
        except Exception:
            return ("persisted_projection_inspection_failed",)
        if not isinstance(projection, PersistedRuntimeProjection):
            return ("persisted_projection_contract_invalid",)
        if projection.contract != PERSISTED_RUNTIME_PROJECTION_CONTRACT:
            return ("persisted_projection_contract_invalid",)
        if projection.runtime_key != runtime.key:
            rejections.append("resolved_runtime_identity_mismatch")
        if projection.resolution_state != "resolved":
            rejections.append("projected_resolution_state_mismatch")
        if projection.model != runtime.key.model:
            rejections.append("projected_model_mismatch")
        if projection.provider != runtime.key.provider:
            rejections.append("projected_provider_mismatch")
        if projection.api_mode != runtime.key.api_mode:
            rejections.append("projected_api_mode_mismatch")
        if (
            projection.credential_pool_identity
            != runtime.key.credential_pool_identity
        ):
            rejections.append("projected_credential_pool_mismatch")
        if not projection.resolver_name:
            rejections.append("projected_resolver_missing")
        if not projection.access_kind:
            rejections.append("projected_access_kind_missing")
        if projection.reasoning_effort != reasoning_effort:
            rejections.append("projected_reasoning_mismatch")
        if projection.fallback_owner != "auto-routing-pre-call":
            rejections.append("projected_fallback_owner_mismatch")
        if projection.fallback_count != 0:
            rejections.append("projected_fallback_not_empty")
        return tuple(dict.fromkeys(rejections))

    def _doctor_exact_projection_contracts(
        self,
        proposal: AutoRoutingConfig,
        inventory: Any,
    ) -> tuple[bool, dict[str, Any]]:
        current = {
            runtime.key.stable_id(): runtime
            for runtime in inventory.observations
        }
        targets: list[tuple[str, RoutingTarget, RouteProfile | None]] = []
        for profile in proposal.profiles.values():
            targets.extend(
                (
                    (
                        f"{profile.profile_id}:primary"
                        if index == 0
                        else f"{profile.profile_id}:primary-challenger:{index - 1}"
                    ),
                    target,
                    profile,
                )
                for index, target in enumerate(profile.primary_choices())
            )
            targets.extend(
                (f"{profile.profile_id}:fallback:{index}", target, profile)
                for index, target in enumerate(profile.fallbacks)
            )
        if not isinstance(proposal.safe_default, str):
            targets.append(("safe-default", proposal.safe_default, None))

        details: dict[str, Any] = {}
        healthy = True
        root_config = self._runtime_root_config()
        catalog = CatalogService(store=self.store)
        now = datetime.now(UTC)
        successful_projection_inspections: dict[tuple[str, str], str] = {}
        for label, target, profile in targets:
            runtime_id = target.runtime.stable_id()
            runtime = current.get(runtime_id)
            if runtime is None or runtime.state != "verified":
                healthy = False
                details[label] = {
                    "runtime_id": runtime_id,
                    "status": "not_verified",
                }
                continue
            try:
                verification_current = (
                    runtime.verification_expires_at is not None
                    and _parse_timestamp(runtime.verification_expires_at) > now
                )
            except (TypeError, ValueError):
                verification_current = False
            if not verification_current:
                healthy = False
                details[label] = {
                    "runtime_id": runtime_id,
                    "status": "verification_expired",
                }
                continue
            executable = self._persisted_executable_runtime(runtime)
            rejections = list(
                runtime_policy_rejection_reasons(
                    executable,
                    policy=proposal.policy,
                    catalog=catalog,
                )
            )
            if profile is not None:
                minimum_context = max(
                    proposal.policy.minimum_context_tokens,
                    (
                        profile.limits.minimum_context_tokens
                        if profile.limits is not None
                        and profile.limits.minimum_context_tokens is not None
                        else 0
                    ),
                )
                rejections.extend(
                    runtime_capability_rejection_reasons(
                        executable,
                        required_capabilities=profile.match.capabilities,
                        required_modalities=profile.match.modalities,
                        minimum_context_tokens=minimum_context,
                    )
                )
                if (
                    runtime.key.local_backend
                    and profile.limits is not None
                    and profile.limits.allowed_licenses is not None
                    and runtime.capabilities.get("license_id")
                    not in profile.limits.allowed_licenses
                ):
                    rejections.append("license_not_allowed")

            economics = runtime.economics
            if economics.billing_kind in {"metered", "subscription"}:
                effective_costs = tuple(
                    value
                    for value in (
                        economics.effective_marginal_cost_usd_per_task,
                        economics.effective_amortized_cost_usd_per_task,
                    )
                    if value is not None
                )
                cost = max(effective_costs) if effective_costs else None
            else:
                local_costs = tuple(
                    value
                    for value in (
                        economics.local_compute_cost_usd_per_task,
                        economics.local_energy_cost_usd_per_task,
                    )
                    if value is not None
                )
                cost = sum(local_costs) if local_costs else None
            cost_limit = min(
                value
                for value in (
                    proposal.policy.max_estimated_task_cost_usd,
                    target.max_estimated_task_cost_usd,
                    (
                        profile.limits.max_estimated_task_cost_usd
                        if profile is not None and profile.limits is not None
                        else None
                    ),
                )
                if value is not None
            )
            if cost is None:
                rejections.append("estimated_cost_unknown")
            elif cost > cost_limit:
                rejections.append("estimated_cost_exceeds_limit")

            latency_limit = min(
                value
                for value in (
                    proposal.policy.max_estimated_latency_seconds,
                    target.max_estimated_latency_seconds,
                    (
                        profile.limits.max_estimated_latency_seconds
                        if profile is not None and profile.limits is not None
                        else None
                    ),
                )
                if value is not None
            )
            latency_rows = tuple(
                row
                for row in catalog.evidence_for(executable)
                if row.metric_name == "latency"
                and not catalog.evidence_is_expired(row)
            )
            if not latency_rows:
                rejections.append("estimated_latency_unknown")
            elif max(row.value for row in latency_rows) > latency_limit:
                rejections.append("estimated_latency_exceeds_limit")
            support = executable.reasoning_support
            aliases = dict(support.provider_aliases)

            def supports(effort: str) -> bool:
                return effort in support.efforts or aliases.get(effort) in support.efforts

            if not support.exact or not all(
                supports(effort)
                for effort in (
                    target.reasoning.minimum,
                    target.reasoning.default,
                    target.reasoning.maximum,
                )
            ):
                rejections.append("reasoning_unsupported")
            maximum_reasoning = min(
                REASONING_EFFORT_ORDER.index(proposal.policy.max_reasoning_effort),
                REASONING_EFFORT_ORDER.index(
                    profile.limits.max_reasoning_effort
                    if profile is not None
                    and profile.limits is not None
                    and profile.limits.max_reasoning_effort is not None
                    else proposal.policy.max_reasoning_effort
                ),
            )
            if any(
                REASONING_EFFORT_ORDER.index(effort) > maximum_reasoning
                for effort in (
                    target.reasoning.minimum,
                    target.reasoning.default,
                    target.reasoning.maximum,
                )
            ):
                rejections.append("reasoning_out_of_bounds")
            projection_rejections = (
                self._persisted_projection_rejection_reasons(
                    runtime,
                    reasoning_effort=target.reasoning.default,
                    root_config=root_config,
                )
            )
            rejections.extend(projection_rejections)
            if not projection_rejections:
                successful_projection_inspections.setdefault(
                    (runtime_id, target.reasoning.default),
                    label,
                )
            rejections = list(dict.fromkeys(rejections))
            if rejections:
                healthy = False
                details[label] = {
                    "runtime_id": runtime_id,
                    "status": "projection_error",
                    "reasons": rejections,
                }
            else:
                details[label] = {
                    "runtime_id": runtime_id,
                    "status": "ok",
                    "reasoning_effort": target.reasoning.default,
                    "fallback_count": 0,
                }
        if isinstance(proposal.safe_default, str):
            status, detail, runtime_id = self._doctor_inherited_safe_default(
                proposal,
                inventory,
            )
            if status != "ok" or runtime_id is None:
                details["safe-default"] = detail
                healthy = False
            else:
                runtime = current[runtime_id]
                configured_reasoning = resolve_reasoning_config(
                    dict(root_config),
                    runtime.key.model,
                )
                requested_effort = effective_generic_reasoning_effort(
                    configured_reasoning
                )
                inherited_effort, _supported = (
                    self._inherited_safe_default_reasoning(
                        self._persisted_executable_runtime(
                            runtime
                        ).reasoning_support,
                        requested_effort,
                    )
                )
                existing_label = successful_projection_inspections.get(
                    (runtime_id, inherited_effort)
                )
                if existing_label is not None:
                    details["safe-default"] = {
                        "runtime_id": runtime_id,
                        "status": "ok",
                        "reasoning_effort": inherited_effort,
                        "fallback_count": 0,
                        "projection_reused_from": existing_label,
                    }
                else:
                    rejections = list(
                        self._persisted_projection_rejection_reasons(
                            runtime,
                            reasoning_effort=inherited_effort,
                            root_config=root_config,
                        )
                    )
                    if rejections:
                        healthy = False
                        details["safe-default"] = {
                            "runtime_id": runtime_id,
                            "status": "projection_error",
                            "reasons": rejections,
                        }
                    else:
                        details["safe-default"] = {
                            "runtime_id": runtime_id,
                            "status": "ok",
                            "reasoning_effort": inherited_effort,
                            "fallback_count": 0,
                        }
        return healthy, details

    @staticmethod
    def _persisted_executable_runtime(observation: Any) -> ExecutableRuntime:
        support = resolve_reasoning_support(
            provider=observation.key.provider,
            model=observation.key.model,
            api_mode=observation.key.api_mode,
            metadata=dict(observation.capabilities),
        )
        return ExecutableRuntime(
            key=observation.key,
            resolver_name=observation.key.provider,
            state=observation.state,
            reasons=ReasonCodes(observation.reasons),
            economics=observation.economics,
            reasoning_support=support,
            verification_source=observation.verification_source,
            verified_at=observation.verified_at,
            verification_expires_at=observation.verification_expires_at,
            provenance=observation.provenance,
            observed_at=observation.observed_at,
            capabilities=dict(observation.capabilities),
        )

    def _doctor_classifier_contract(
        self,
        proposal: AutoRoutingConfig,
        inventory: Any,
    ) -> tuple[bool, dict[str, Any]]:
        detail = {
            "provider": proposal.classifier.provider,
            "model": proposal.classifier.model,
            "reasoning_effort": proposal.classifier.reasoning_effort,
            "maximum_daily_overhead_usd": (
                proposal.policy.max_routing_overhead_usd_per_day
            ),
        }
        if inventory is None:
            return False, {**detail, "reason": "inventory_unavailable"}
        provider = proposal.classifier.provider.casefold()
        model = proposal.classifier.model.casefold()
        allowed_providers = {
            value.casefold() for value in proposal.llm.allowed_providers
        }
        allowed_models = {
            value.casefold() for value in proposal.llm.allowed_models
        }
        if (
            not proposal.llm.allow_provider_override
            or not proposal.llm.allow_model_override
            or ("*" not in allowed_providers and provider not in allowed_providers)
            or ("*" not in allowed_models and model not in allowed_models)
            or provider in {value.casefold() for value in proposal.policy.denied_providers}
            or model in {value.casefold() for value in proposal.policy.denied_models}
            or provider == "moa"
        ):
            return False, {**detail, "reason": "classifier_trust_denied"}
        matches = tuple(
            observation
            for observation in inventory.observations
            if observation.key.provider.casefold() == provider
            and observation.key.model.casefold() == model
        )
        if len(matches) != 1:
            return False, {**detail, "reason": "classifier_runtime_not_exact"}
        observation = matches[0]
        now = datetime.now(UTC)
        try:
            verified_at = _parse_timestamp(str(observation.verified_at))
            expires_at = _parse_timestamp(str(observation.verification_expires_at))
        except (TypeError, ValueError):
            return False, {**detail, "reason": "classifier_verification_invalid"}
        if (
            observation.state != "verified"
            or observation.verification_source is None
            or verified_at > now
            or expires_at <= now
        ):
            return False, {**detail, "reason": "classifier_verification_stale"}
        runtime = self._persisted_executable_runtime(observation)
        effort = proposal.classifier.reasoning_effort
        aliases = dict(runtime.reasoning_support.provider_aliases)
        if (
            not runtime.reasoning_support.exact
            or (
                effort not in runtime.reasoning_support.efforts
                and aliases.get(effort) not in runtime.reasoning_support.efforts
            )
        ):
            return False, {**detail, "reason": "classifier_reasoning_unsupported"}
        economics = observation.economics
        ttl = economics.evidence_ttl_seconds
        try:
            observed_at = _parse_timestamp(economics.observed_at)
        except (TypeError, ValueError):
            return False, {**detail, "reason": "classifier_economics_invalid"}
        if ttl is None or observed_at > now or now - observed_at > timedelta(seconds=ttl):
            return False, {**detail, "reason": "classifier_economics_stale"}
        if economics.billing_kind == "metered":
            input_price = economics.metered_input_usd_per_million_tokens
            output_price = economics.metered_output_usd_per_million_tokens
            if input_price is None or output_price is None:
                return False, {**detail, "reason": "classifier_economics_unknown"}
            cost = (
                proposal.classifier.maximum_input_tokens * input_price
                + proposal.classifier.maximum_output_tokens * output_price
            ) / 1_000_000
        elif economics.billing_kind == "subscription":
            values = (
                economics.effective_marginal_cost_usd_per_task,
                economics.effective_amortized_cost_usd_per_task,
            )
            remaining = economics.subscription_quota_remaining
            if (
                not proposal.policy.allow_subscription
                or any(value is None for value in values)
                or economics.subscription_plan is None
                or economics.subscription_quota_unit is None
                or str(economics.subscription_state or "").casefold() != "active"
                or remaining is None
                or float(remaining) <= 0
            ):
                return False, {**detail, "reason": "classifier_economics_unknown"}
            cost = max(float(value) for value in values if value is not None)
        else:
            energy = economics.local_energy_cost_usd_per_task
            compute = economics.local_compute_cost_usd_per_task
            if energy is None or compute is None:
                return False, {**detail, "reason": "classifier_economics_unknown"}
            cost = float(energy) + float(compute)
        if (
            not math.isfinite(float(cost))
            or float(cost) < 0
            or float(cost) > proposal.policy.max_routing_overhead_usd_per_day
        ):
            return False, {**detail, "reason": "classifier_economics_exceeds_limit"}
        return True, {**detail, "worst_case_cost_usd": float(cost)}

    def _doctor_inherited_safe_default(
        self,
        proposal: AutoRoutingConfig,
        inventory: Any,
    ) -> tuple[str, dict[str, Any], str | None]:
        try:
            if inventory is None:
                raise AutoRoutingServiceError("inherit_runtime_unresolvable")
            observations = tuple(inventory.observations)
            identifier = getattr(
                self.adapter,
                "identify_persisted_inherited_runtime",
                None,
            )
            if callable(identifier):
                runtime_key = identifier(
                    observations,
                    self._runtime_root_config(),
                )
            else:
                verified = tuple(
                    item for item in observations if item.state == "verified"
                )
                runtime_key = verified[0].key if len(verified) == 1 else None
            if runtime_key is None:
                raise AutoRoutingServiceError("inherit_runtime_unresolvable")
            runtime_id = runtime_key.stable_id()
            matches = [
                runtime
                for runtime in observations
                if runtime.key.stable_id() == runtime_id
            ]
            if len(matches) != 1:
                raise AutoRoutingServiceError("inherit_runtime_ambiguous")
            runtime = matches[0]
            if runtime.state != "verified":
                raise AutoRoutingServiceError("inherit_runtime_not_verified")
            expires_at = runtime.verification_expires_at
            if expires_at is None or _parse_timestamp(expires_at) <= datetime.now(UTC):
                raise AutoRoutingServiceError("inherit_runtime_evidence_stale")
            catalog = CatalogService(store=self.store)
            policy_reasons = runtime_policy_rejection_reasons(
                self._persisted_executable_runtime(runtime),
                policy=proposal.policy,
                catalog=catalog,
            )
            if policy_reasons:
                return (
                    "error",
                    {
                        "mode": "inherit",
                        "runtime_id": runtime_id,
                        "reason": "inherit_runtime_policy_noncompliant",
                        "reasons": list(policy_reasons),
                    },
                    runtime_id,
                )
            return (
                "ok",
                {
                    "mode": "inherit",
                    "runtime_id": runtime_id,
                    "verification_source": runtime.verification_source,
                    "policy_compliant": True,
                },
                runtime_id,
            )
        except Exception as error:
            reason = str(error) or "inherit_runtime_unresolvable"
            if reason not in {
                "inherit_runtime_unresolvable",
                "inherit_runtime_ambiguous",
                "inherit_runtime_not_verified",
                "inherit_runtime_evidence_stale",
            }:
                reason = "inherit_runtime_unresolvable"
            return (
                "error",
                {"mode": "inherit", "reason": reason},
                None,
            )

    def _doctor_explicit_safe_default(
        self,
        proposal: AutoRoutingConfig,
        inventory: Any,
    ) -> tuple[str, dict[str, Any]]:
        assert not isinstance(proposal.safe_default, str)
        runtime_id = proposal.safe_default.runtime.stable_id()
        try:
            if inventory is None:
                raise AutoRoutingServiceError("explicit_runtime_unresolvable")
            matches = [
                runtime
                for runtime in inventory.observations
                if runtime.key.stable_id() == runtime_id
            ]
            if len(matches) != 1:
                raise AutoRoutingServiceError("explicit_runtime_unresolvable")
            runtime = matches[0]
            if runtime.state != "verified":
                raise AutoRoutingServiceError("explicit_runtime_not_verified")
            expires_at = runtime.verification_expires_at
            if expires_at is None or _parse_timestamp(expires_at) <= datetime.now(UTC):
                raise AutoRoutingServiceError("explicit_runtime_evidence_stale")
            policy_reasons = runtime_policy_rejection_reasons(
                self._persisted_executable_runtime(runtime),
                policy=proposal.policy,
                catalog=CatalogService(store=self.store),
            )
            if policy_reasons:
                return (
                    "error",
                    {
                        "mode": "explicit",
                        "runtime_id": runtime_id,
                        "reason": "explicit_runtime_policy_noncompliant",
                        "reasons": list(policy_reasons),
                    },
                )
            return (
                "ok",
                {
                    "mode": "explicit",
                    "runtime_id": runtime_id,
                    "verification_source": runtime.verification_source,
                    "policy_compliant": True,
                },
            )
        except Exception as error:
            reason = str(error) or "explicit_runtime_unresolvable"
            if reason not in {
                "explicit_runtime_unresolvable",
                "explicit_runtime_not_verified",
                "explicit_runtime_evidence_stale",
            }:
                reason = "explicit_runtime_unresolvable"
            return "error", {"mode": "explicit", "reason": reason}


__all__ = ["AutoRoutingService", "AutoRoutingServiceError"]
