"""Read-only previews and guarded atomic writes for auto-routing config."""

from __future__ import annotations

import difflib
import hashlib
import hmac
import json
import os
import stat
import tempfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from hermes_cli import managed_scope
from hermes_cli.config import require_readable_config_before_write
from hermes_constants import get_config_path
from utils import atomic_roundtrip_yaml_update, fast_safe_load

from .config import (
    ConfigError,
    authority_document,
    config_document,
    config_revision,
    parse_config,
)
from .config import (
    authority_revision as compute_authority_revision,
)
from .models import AutoRoutingConfig
from .models import (
    AdaptiveRevision,
    ManagementConfigReceipt,
    ManagementControl,
    ManagementLifecycleFinalization,
    ManagementLifecycleEvent,
    ManagementPatch,
    ManagementProfileState,
    ManagementRevision,
)
from .storage import (
    ActivationReceipt,
    ImmutableRecordConflict,
    RevisionConflict,
    RoutingStore,
    management_recovery_event_id,
    management_restore_started_event_id,
)

if os.name == "nt":
    import msvcrt
else:
    import fcntl


CANONICAL_APPLY_COMMAND = "hermes auto-routing setup --apply"
MANAGED_SUBTREE = "plugins.entries.auto-routing"
BACKUP_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"


class ConfigIOError(RuntimeError):
    """Base error for guarded auto-routing configuration updates."""


class ConfigConflict(ConfigIOError):
    """Raised when an approved preview no longer matches the requested write."""


class ManagedConfigError(ConfigIOError):
    """Raised when managed configuration owns any part of the plugin subtree."""


class ConfigVerificationError(ConfigIOError):
    """Raised when the replaced file does not exactly match the approved preview."""


class ConfigRollbackError(ConfigIOError):
    """Raised when both an apply operation and its recovery attempt fail."""

    def __init__(self, backup_path: Path, original_error: BaseException) -> None:
        self.backup_path = backup_path
        self.original_error = original_error
        super().__init__(
            "config apply failed and rollback could not restore the original; "
            f"recovery backup remains at {backup_path}"
        )


@dataclass(frozen=True, slots=True)
class ConfigPreview:
    """Exact, deterministic before/after evidence for one proposed update."""

    config_path: Path
    before_bytes: bytes
    after_bytes: bytes
    unified_diff: str
    before_sha256: str
    after_sha256: str
    authority_revision: str
    backup_filename_pattern: str
    precondition_sha256: str

    @property
    def path(self) -> Path:
        """Compatibility alias for callers that display the target path."""
        return self.config_path

    @property
    def diff(self) -> str:
        """Compatibility alias for the unified textual diff."""
        return self.unified_diff

    @property
    def backup_path_pattern(self) -> Path:
        """Return the previewed backup pattern next to the config file."""
        return self.config_path.with_name(self.backup_filename_pattern)


@dataclass(frozen=True, slots=True)
class AppliedConfig(ConfigPreview):
    """A verified applied preview and the exact recovery backup it created."""

    backup_path: Path


@dataclass(frozen=True, slots=True)
class ManagementRevisionResult:
    """Content-free outcome of one guarded management config revision."""

    changed: bool
    reason_code: str
    revision_id: str | None


@dataclass(frozen=True, slots=True)
class ManagementActivationRollover:
    """Exact active-authority records derived from one valid predecessor."""

    predecessor: ActivationReceipt
    receipt: ActivationReceipt
    authority_document: Mapping[str, Any]
    baseline: AdaptiveRevision


@dataclass(frozen=True, slots=True)
class _PinnedConfigPath:
    logical_path: Path
    target_path: Path
    logical_lexists: bool
    logical_is_symlink: bool
    target_existed: bool


@dataclass(frozen=True, slots=True)
class _PreparedPreview:
    preview: ConfigPreview
    proposal: AutoRoutingConfig


@dataclass(frozen=True, slots=True)
class _FileMetadata:
    mode: int | None
    owner: tuple[int, int] | None


@dataclass(slots=True)
class _MutationState:
    replace_attempted: bool = False
    target_replaced: bool = False


@dataclass(slots=True)
class LockedConfigUpdate:
    """One freshly prepared config mutation held under the profile lock."""

    preview: ConfigPreview
    proposal: AutoRoutingConfig
    _pinned_path: _PinnedConfigPath
    _metadata: _FileMetadata
    _mutation_state: _MutationState
    mutation_guard_reason: str | None = None

    @property
    def source_existed(self) -> bool:
        return self._pinned_path.target_existed

    @property
    def target_path(self) -> Path:
        return self._pinned_path.target_path

    def current_config(self) -> AutoRoutingConfig:
        """Read the current authority from the target pinned by this lock."""
        _assert_pinned_target(
            self._pinned_path,
            before_mutation=True,
            expected_bytes=self.preview.before_bytes,
        )
        try:
            document = fast_safe_load(self._pinned_path.target_path.read_bytes())
        except Exception as error:
            raise ConfigConflict("locked config could not be read") from error
        if not isinstance(document, Mapping):
            raise ConfigConflict("locked config is not a mapping")
        try:
            return parse_config(document)
        except Exception as error:
            raise ConfigConflict("locked config authority is invalid") from error

    def create_backup(self, backup_path: Path) -> None:
        """Create and verify a sibling recovery backup before replacement."""
        if backup_path.parent != self.preview.config_path.parent:
            raise ConfigConflict("config recovery backup must be a sibling")
        _create_backup(backup_path, self.preview.before_bytes)
        if backup_path.read_bytes() != self.preview.before_bytes:
            raise ConfigVerificationError("config recovery backup verification failed")

    def replace(self) -> None:
        """Atomically install and verify the exact previewed after bytes."""
        _assert_pinned_target(
            self._pinned_path,
            before_mutation=True,
            expected_bytes=self.preview.before_bytes,
        )
        _atomic_write_bytes(
            self._pinned_path.target_path,
            self.preview.after_bytes,
            mode=self._metadata.mode,
            owner=self._metadata.owner,
            mutation_state=self._mutation_state,
        )
        _assert_pinned_target(self._pinned_path, before_mutation=False)
        _verify_applied_config(
            self._pinned_path.target_path,
            self.proposal,
            self.preview.after_bytes,
            self.preview.after_sha256,
        )

    def restore(self, backup_path: Path) -> None:
        """Restore and verify the exact previewed before state."""
        if backup_path.parent != self.preview.config_path.parent:
            raise ConfigConflict("config recovery backup must be a sibling")
        try:
            current_bytes = self._pinned_path.target_path.read_bytes()
        except FileNotFoundError:
            target_exists = False
            current_bytes = None
        except OSError as error:
            raise ConfigConflict(
                "config target state is indeterminate; recovery was not attempted"
            ) from error
        else:
            target_exists = True
        original_observed = (
            self._pinned_path.target_existed
            and target_exists
            and current_bytes == self.preview.before_bytes
        ) or (not self._pinned_path.target_existed and not target_exists)
        replacement_observed = (
            target_exists and current_bytes == self.preview.after_bytes
        )
        if original_observed:
            return
        if not replacement_observed:
            raise ConfigConflict(
                "config changed outside the guarded apply; refusing to overwrite it"
            )
        _restore_original(
            self._pinned_path.target_path,
            backup_path,
            source_existed=self._pinned_path.target_existed,
            metadata=self._metadata,
        )
        if self._pinned_path.target_existed:
            actual = self._pinned_path.target_path.read_bytes()
            if actual != self.preview.before_bytes:
                raise ConfigVerificationError("restored config bytes changed")
        elif self._pinned_path.target_path.exists():
            raise ConfigVerificationError("restored absent config still exists")

    def restore_exact_backup(
        self,
        backup_path: Path,
        *,
        expected_backup_sha256: str,
        expected_current_authority_id: str,
        expected_restored_authority_id: str,
    ) -> None:
        """Restore one receipt-bound backup without overwriting newer authority."""
        if backup_path.parent != self.preview.config_path.parent:
            raise ConfigConflict("config recovery backup must be a sibling")
        try:
            backup_bytes = backup_path.read_bytes()
        except OSError as error:
            raise ConfigConflict("management recovery backup is unavailable") from error
        backup_sha256 = hashlib.sha256(backup_bytes).hexdigest()
        if not hmac.compare_digest(backup_sha256, expected_backup_sha256):
            raise ConfigVerificationError("management recovery backup checksum changed")
        try:
            backup_document = fast_safe_load(backup_bytes)
            if not isinstance(backup_document, Mapping):
                raise ValueError("backup root is not a mapping")
            backup_config = parse_config(backup_document)
        except Exception as error:
            raise ConfigVerificationError(
                "management recovery backup authority is invalid"
            ) from error
        if not hmac.compare_digest(
            compute_authority_revision(backup_config),
            expected_restored_authority_id,
        ):
            raise ConfigVerificationError(
                "management recovery backup has a different authority"
            )
        current = self.current_config()
        if not hmac.compare_digest(
            compute_authority_revision(current),
            expected_current_authority_id,
        ):
            raise ConfigConflict(
                "config changed outside management recovery; refusing overwrite"
            )
        if self._pinned_path.target_path.read_bytes() == backup_bytes:
            return
        _assert_pinned_target(
            self._pinned_path,
            before_mutation=True,
            expected_bytes=self.preview.before_bytes,
        )
        _atomic_write_bytes(
            self._pinned_path.target_path,
            backup_bytes,
            mode=self._metadata.mode,
            owner=self._metadata.owner,
            mutation_state=self._mutation_state,
        )
        _assert_pinned_target(self._pinned_path, before_mutation=False)
        if self._pinned_path.target_path.read_bytes() != backup_bytes:
            raise ConfigVerificationError("restored management config bytes changed")
        restored_document = fast_safe_load(self._pinned_path.target_path.read_bytes())
        if not isinstance(restored_document, Mapping):
            raise ConfigVerificationError("restored management config is not a mapping")
        restored = parse_config(restored_document)
        if not hmac.compare_digest(
            compute_authority_revision(restored),
            expected_restored_authority_id,
        ):
            raise ConfigVerificationError(
                "restored management config authority changed"
            )


_thread_locks_guard = threading.Lock()
_thread_locks: dict[str, threading.Lock] = {}


def preview_update(
    proposal: AutoRoutingConfig,
    path: str | os.PathLike[str] | None = None,
    *,
    allow_active: bool = False,
) -> ConfigPreview:
    """Preview an exact round-trip YAML update without touching the real file."""
    logical_path = _config_path(path)
    return _prepare_preview(
        proposal,
        _pin_config_path(logical_path),
        allow_active=allow_active,
    ).preview


@contextmanager
def profile_config_lock(
    path: str | os.PathLike[str] | None = None,
) -> Iterator[None]:
    """Hold the logical and resolved config locks for one profile operation."""
    logical_path = _config_path(path)
    candidate_target = Path(_resolve_target_path(logical_path)).expanduser().absolute()
    with _exclusive_config_locks(logical_path, candidate_target):
        pinned_path = _pin_config_path(logical_path)
        if _path_identity(pinned_path.target_path) != _path_identity(candidate_target):
            raise ConfigConflict("config target changed while acquiring apply locks")
        yield


@contextmanager
def locked_update(
    proposal: AutoRoutingConfig,
    path: str | os.PathLike[str] | None = None,
    *,
    allow_active: bool = False,
    mutation_guard: Callable[[AutoRoutingConfig], Any] | None = None,
) -> Iterator[LockedConfigUpdate]:
    """Prepare and hold one update under the same locks used by apply."""
    logical_path = _config_path(path)
    with profile_config_lock(logical_path):
        pinned_path = _pin_config_path(logical_path)
        prepared = _prepare_preview(
            proposal,
            pinned_path,
            allow_active=allow_active,
        )
        update = LockedConfigUpdate(
            preview=prepared.preview,
            proposal=prepared.proposal,
            _pinned_path=pinned_path,
            _metadata=_file_metadata(pinned_path.target_path),
            _mutation_state=_MutationState(),
        )
        if mutation_guard is None:
            yield update
        else:
            current = update.current_config()
            with mutation_guard(current) as reason:
                update.mutation_guard_reason = (
                    None if reason is None else str(reason)
                )
                yield update


def apply_update(
    proposal: AutoRoutingConfig,
    expected_precondition_sha256: str,
    path: str | os.PathLike[str] | None = None,
) -> AppliedConfig:
    """Apply an approved preview under process and cross-process exclusion."""
    logical_path = _config_path(path)
    candidate_target = Path(_resolve_target_path(logical_path)).expanduser().absolute()
    with _exclusive_config_locks(logical_path, candidate_target):
        pinned_path = _pin_config_path(logical_path)
        if _path_identity(pinned_path.target_path) != _path_identity(candidate_target):
            raise ConfigConflict("config target changed while acquiring apply locks")
        prepared = _prepare_preview(proposal, pinned_path)
        preview = prepared.preview
        if not _hashes_match(
            expected_precondition_sha256,
            preview.precondition_sha256,
        ):
            raise ConfigConflict(
                "config apply precondition changed; request and approve a new preview"
            )

        _assert_pinned_target(
            pinned_path,
            before_mutation=True,
            expected_bytes=preview.before_bytes,
        )
        timestamp = datetime.now(timezone.utc).strftime(BACKUP_TIMESTAMP_FORMAT)
        backup_path = logical_path.with_name(
            f"{logical_path.name}.auto-routing.{timestamp}.bak"
        )
        metadata = _file_metadata(pinned_path.target_path)
        _create_backup(backup_path, preview.before_bytes)

        mutation_state = _MutationState()
        try:
            _assert_pinned_target(
                pinned_path,
                before_mutation=True,
                expected_bytes=preview.before_bytes,
            )
            _atomic_write_bytes(
                pinned_path.target_path,
                preview.after_bytes,
                mode=metadata.mode,
                owner=metadata.owner,
                mutation_state=mutation_state,
            )
            _assert_pinned_target(pinned_path, before_mutation=False)
            _verify_applied_config(
                pinned_path.target_path,
                prepared.proposal,
                preview.after_bytes,
                preview.after_sha256,
            )
        except BaseException as original_error:
            try:
                target_replaced = _replacement_observed_after_error(
                    pinned_path,
                    preview.before_bytes,
                    preview.after_bytes,
                    mutation_state,
                )
            except ConfigConflict as reconciliation_error:
                raise reconciliation_error from original_error
            if not target_replaced:
                raise
            try:
                _restore_original(
                    pinned_path.target_path,
                    backup_path,
                    source_existed=pinned_path.target_existed,
                    metadata=metadata,
                )
            except BaseException as rollback_error:
                raise ConfigRollbackError(
                    backup_path, original_error
                ) from rollback_error
            raise

        return AppliedConfig(
            config_path=preview.config_path,
            before_bytes=preview.before_bytes,
            after_bytes=preview.after_bytes,
            unified_diff=preview.unified_diff,
            before_sha256=preview.before_sha256,
            after_sha256=preview.after_sha256,
            authority_revision=preview.authority_revision,
            backup_filename_pattern=preview.backup_filename_pattern,
            precondition_sha256=preview.precondition_sha256,
            backup_path=backup_path,
        )


def _management_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _management_receipt_id(revision_id: str) -> str:
    return _management_hash({
        "kind": "management-config-receipt-v1",
        "revision_id": revision_id,
    })


def _management_backup_path(config_path: Path, receipt_id: str) -> Path:
    return config_path.with_name(
        f"{config_path.name}.auto-routing.management.{receipt_id}.bak"
    )


def _management_runtime_ids(
    config: AutoRoutingConfig,
    profile_id: str,
    fallback: tuple[str, ...],
) -> tuple[str, ...]:
    profile = config.profiles.get(profile_id)
    if profile is None:
        return fallback
    return tuple(
        target.runtime.stable_id()
        for target in (*profile.primary_choices(), *profile.fallbacks)
    )


def _management_event_id(
    *,
    kind: str,
    revision_id: str,
    profile_id: str,
    reason_code: str,
) -> str:
    return _management_hash({
        "kind": kind,
        "revision_id": revision_id,
        "profile_id": profile_id,
        "reason_code": reason_code,
    })


def _transition_management_receipt(
    store: RoutingStore,
    receipt: ManagementConfigReceipt,
    phase: str,
) -> ManagementConfigReceipt:
    return store.recover_management_receipt(
        receipt.model_copy(update={"phase": phase, "updated_at": receipt.updated_at}),
        expected_phase=receipt.phase,
    )


def _mark_management_receipt_recovery_required(
    store: RoutingStore,
    receipt: ManagementConfigReceipt,
) -> ManagementConfigReceipt:
    current = store.read_management_receipt(receipt.receipt_id)
    if current is None:
        return receipt
    if current.phase == "recovery_required":
        return current
    if current.phase == "committed":
        return current
    return _transition_management_receipt(store, current, "recovery_required")


def _claim_management_receipt_recovery(
    store: RoutingStore,
    approved: ManagementConfigReceipt,
) -> ManagementConfigReceipt:
    """CAS the exact approved receipt into its terminal recovery claim."""
    current = store.read_management_receipt(approved.receipt_id)
    if current is None or current != approved or current.phase == "committed":
        raise ConfigConflict(
            "management recovery precondition changed; run the preview again"
        )
    if current.phase == "recovery_required":
        return current
    try:
        return _transition_management_receipt(
            store,
            current,
            "recovery_required",
        )
    except (ImmutableRecordConflict, RevisionConflict) as error:
        raise ConfigConflict(
            "management recovery precondition changed; run the preview again"
        ) from error


def _management_recovery_revision(
    *,
    receipt: ManagementConfigReceipt,
    failed: ManagementRevision,
) -> ManagementRevision:
    """Describe the exact failed-resulting to restored-preceding reversal."""
    patches = tuple(
        ManagementPatch(
            profile_id=patch.profile_id,
            before_runtime_ids=patch.after_runtime_ids,
            after_runtime_ids=patch.before_runtime_ids,
            reason_codes=("config_recovered",),
        )
        for patch in failed.patches
    )
    seed = {
        "kind": "management-config-recovery-revision-v1",
        "receipt_id": receipt.receipt_id,
        "failed_revision_id": failed.revision_id,
        "preceding_authority_id": failed.resulting_authority_id,
        "resulting_authority_id": failed.preceding_authority_id,
        "backup_checksum": receipt.backup_checksum,
        "patches": [patch.model_dump(mode="json") for patch in patches],
    }
    return ManagementRevision(
        revision_id=_management_hash(seed),
        preceding_authority_id=failed.resulting_authority_id,
        resulting_authority_id=failed.preceding_authority_id,
        management_authority_id=failed.management_authority_id,
        parent_revision_id=failed.revision_id,
        ranking_pack=failed.ranking_pack,
        inventory_revision=failed.inventory_revision,
        inventory_fingerprint=failed.inventory_fingerprint,
        management_epoch=failed.management_epoch + 1,
        action="recovery",
        patches=patches,
        runtime_scores=(),
        created_at=receipt.updated_at,
    )


def _management_recovery_is_complete(
    *,
    receipt: ManagementConfigReceipt,
    revision: ManagementRevision,
    store: RoutingStore,
) -> bool:
    """Return whether every affected profile has receipt-bound terminal evidence."""
    return all(
        any(
            event.event_id
            == management_recovery_event_id(
                receipt_id=receipt.receipt_id,
                failed_revision_id=revision.revision_id,
                profile_id=patch.profile_id,
                restored_authority_id=receipt.preceding_authority_id,
                backup_checksum=receipt.backup_checksum,
            )
            for event in store.list_management_lifecycle_events(
                revision.management_authority_id,
                patch.profile_id,
            )
        )
        for patch in revision.patches
    )


def management_config_recovery_complete(
    *,
    receipt: ManagementConfigReceipt,
    revision: ManagementRevision,
    store: RoutingStore,
) -> bool:
    """Public read-only terminal check for recovery orchestration."""
    return _management_recovery_is_complete(
        receipt=receipt,
        revision=revision,
        store=store,
    )


def _record_management_restore_started(
    *,
    receipt: ManagementConfigReceipt,
    failed: ManagementRevision,
    store: RoutingStore,
) -> None:
    """Commit receipt-bound resulting-authority intent before restore I/O."""
    with store.write_txn():
        for patch in failed.patches:
            store.record_management_restore_started_event(
                receipt_id=receipt.receipt_id,
                failed_revision_id=failed.revision_id,
                restored_authority_id=receipt.preceding_authority_id,
                backup_checksum=receipt.backup_checksum,
                event=ManagementLifecycleEvent(
                    event_id=management_restore_started_event_id(
                        receipt_id=receipt.receipt_id,
                        failed_revision_id=failed.revision_id,
                        profile_id=patch.profile_id,
                        restored_authority_id=receipt.preceding_authority_id,
                        backup_checksum=receipt.backup_checksum,
                    ),
                    management_authority_id=failed.management_authority_id,
                    profile_id=patch.profile_id,
                    revision_id=failed.revision_id,
                    event_type="hold",
                    reason_code="config_restore_started",
                    created_at=receipt.updated_at,
                ),
            )


def _management_restore_was_started(
    *,
    receipt: ManagementConfigReceipt,
    failed: ManagementRevision,
    store: RoutingStore,
) -> bool:
    """Prove every profile shared the same durable pre-I/O recovery intent."""
    return all(
        any(
            event.event_id
            == management_restore_started_event_id(
                receipt_id=receipt.receipt_id,
                failed_revision_id=failed.revision_id,
                profile_id=patch.profile_id,
                restored_authority_id=receipt.preceding_authority_id,
                backup_checksum=receipt.backup_checksum,
            )
            for event in store.list_management_lifecycle_events(
                failed.management_authority_id,
                patch.profile_id,
            )
        )
        for patch in failed.patches
    )


def _management_recovery_predecessor_was_eligible(
    *,
    receipt: ManagementConfigReceipt,
    failed: ManagementRevision,
    profile_id: str,
    store: RoutingStore,
) -> bool:
    """Prove an overwritten recovery hold followed an eligible profile state."""
    current_hold_id = _management_event_id(
        kind="management-config-recovery-required-v1",
        revision_id=failed.revision_id,
        profile_id=profile_id,
        reason_code="config_recovery_failed",
    )
    restore_started_id = management_restore_started_event_id(
        receipt_id=receipt.receipt_id,
        failed_revision_id=failed.revision_id,
        profile_id=profile_id,
        restored_authority_id=receipt.preceding_authority_id,
        backup_checksum=receipt.backup_checksum,
    )
    eligible_at: datetime | None = None
    noneligible_at: datetime | None = None
    for event in store.list_management_lifecycle_events(
        failed.management_authority_id,
        profile_id,
    ):
        if event.event_id in {current_hold_id, restore_started_id}:
            continue
        moment = datetime.fromisoformat(event.created_at.replace("Z", "+00:00"))
        eligible_event = event.event_type == "proposed" or (
            event.event_type == "recovered"
            and event.revision_id is not None
            and event.event_id
            == _management_event_id(
                kind="management-config-recovery-state-v1",
                revision_id=event.revision_id,
                profile_id=profile_id,
                reason_code="config_recovered",
            )
        )
        if eligible_event:
            eligible_at = moment if eligible_at is None else max(eligible_at, moment)
        elif event.event_type in {
            "validated",
            "canary",
            "promoted",
            "rejected",
            "rolled_back",
            "cooldown",
            "hold",
        }:
            noneligible_at = (
                moment if noneligible_at is None else max(noneligible_at, moment)
            )
    if noneligible_at is None:
        return True
    return eligible_at is not None and eligible_at > noneligible_at


def _finalize_management_recovery(
    *,
    receipt: ManagementConfigReceipt,
    failed: ManagementRevision,
    observed_authority_id: str,
    store: RoutingStore,
) -> None:
    """Atomically record truthful recovery history and safe profile state."""
    restore_was_started = _management_restore_was_started(
        receipt=receipt,
        failed=failed,
        store=store,
    )
    recovery = (
        _management_recovery_revision(receipt=receipt, failed=failed)
        if observed_authority_id == failed.resulting_authority_id
        or (
            observed_authority_id == failed.preceding_authority_id
            and restore_was_started
        )
        else None
    )
    with store.write_txn():
        if recovery is not None:
            store.publish_management_revision(recovery)
        for patch in failed.patches:
            current = store.read_management_profile_state(
                failed.management_authority_id,
                patch.profile_id,
                current_authority_id=failed.preceding_authority_id,
            )
            linked_revision_id = current.active_revision_id
            can_return_to_eligible = current.experiment_phase == "eligible" or (
                current.experiment_phase == "recovery_required"
                and _management_recovery_predecessor_was_eligible(
                    receipt=receipt,
                    failed=failed,
                    profile_id=patch.profile_id,
                    store=store,
                )
            )
            if recovery is not None and can_return_to_eligible:
                state_event = ManagementLifecycleEvent(
                    event_id=_management_event_id(
                        kind="management-config-recovery-state-v1",
                        revision_id=recovery.revision_id,
                        profile_id=patch.profile_id,
                        reason_code="config_recovered",
                    ),
                    management_authority_id=failed.management_authority_id,
                    profile_id=patch.profile_id,
                    revision_id=recovery.revision_id,
                    event_type="recovered",
                    reason_code="config_recovered",
                    created_at=receipt.updated_at,
                )
                store.transition_management_profile_state(
                    profile_id=patch.profile_id,
                    authority_id=failed.management_authority_id,
                    expected_generation=current.generation,
                    state=ManagementProfileState(
                        management_authority_id=failed.management_authority_id,
                        profile_id=patch.profile_id,
                        authority_id=failed.preceding_authority_id,
                        active_revision_id=recovery.revision_id,
                        management_epoch=recovery.management_epoch,
                        experiment_phase="eligible",
                        rejection_count=current.rejection_count,
                        generation=current.generation,
                        updated_at=receipt.updated_at,
                    ),
                    event=state_event,
                )
                linked_revision_id = recovery.revision_id
            terminal_event = ManagementLifecycleEvent(
                event_id=management_recovery_event_id(
                    receipt_id=receipt.receipt_id,
                    failed_revision_id=failed.revision_id,
                    profile_id=patch.profile_id,
                    restored_authority_id=receipt.preceding_authority_id,
                    backup_checksum=receipt.backup_checksum,
                ),
                management_authority_id=failed.management_authority_id,
                profile_id=patch.profile_id,
                revision_id=linked_revision_id,
                event_type="recovered",
                reason_code="config_recovered",
                created_at=receipt.updated_at,
            )
            store.record_management_recovery_event(
                receipt_id=receipt.receipt_id,
                failed_revision_id=failed.revision_id,
                restored_authority_id=receipt.preceding_authority_id,
                backup_checksum=receipt.backup_checksum,
                event=terminal_event,
            )


def _commit_management_revision_state(
    store: RoutingStore,
    revision: ManagementRevision,
) -> None:
    for patch in revision.patches:
        if (
            patch.before_runtime_ids == patch.after_runtime_ids
            and "fallback_primary_challenger" not in patch.reason_codes
        ):
            continue
        current = store.read_management_profile_state(
            revision.management_authority_id,
            patch.profile_id,
            current_authority_id=revision.preceding_authority_id,
        )
        if current.experiment_phase != "eligible":
            raise ConfigConflict("management profile state is not eligible")
        state = ManagementProfileState(
            management_authority_id=revision.management_authority_id,
            profile_id=patch.profile_id,
            authority_id=revision.resulting_authority_id,
            active_revision_id=revision.revision_id,
            management_epoch=revision.management_epoch,
            experiment_phase="eligible",
            rejection_count=current.rejection_count,
            generation=current.generation,
            updated_at=revision.created_at,
        )
        event = ManagementLifecycleEvent(
            event_id=_management_event_id(
                kind="management-revision-applied-v1",
                revision_id=revision.revision_id,
                profile_id=patch.profile_id,
                reason_code=patch.reason_codes[0],
            ),
            management_authority_id=revision.management_authority_id,
            profile_id=patch.profile_id,
            revision_id=revision.revision_id,
            event_type="proposed",
            reason_code=patch.reason_codes[0],
            created_at=revision.created_at,
        )
        store.transition_management_profile_state(
            profile_id=patch.profile_id,
            authority_id=revision.management_authority_id,
            expected_generation=current.generation,
            state=state,
            event=event,
        )


def _materialize_management_control_revision(
    *,
    store: RoutingStore,
    revision: ManagementRevision,
    profile_id: str,
) -> None:
    """Publish and activate one exact control baseline inside the apply txn."""
    if revision.action != "fallback_reorder" or not all(
        item.before_runtime_ids == item.after_runtime_ids for item in revision.patches
    ):
        raise ConfigConflict("management control revision must be a baseline")
    patch = next(
        (item for item in revision.patches if item.profile_id == profile_id),
        None,
    )
    if patch is None:
        raise ConfigConflict("management control baseline does not patch the profile")
    store.publish_management_revision(revision)
    current = store.read_management_profile_state(
        revision.management_authority_id,
        profile_id,
        current_authority_id=revision.resulting_authority_id,
    )
    if current.experiment_phase != "eligible":
        raise ConfigConflict("management profile state is not eligible")
    if current.active_revision_id == revision.revision_id:
        return
    state = ManagementProfileState(
        management_authority_id=revision.management_authority_id,
        profile_id=profile_id,
        authority_id=revision.resulting_authority_id,
        active_revision_id=revision.revision_id,
        management_epoch=revision.management_epoch,
        experiment_phase="eligible",
        rejection_count=current.rejection_count,
        generation=current.generation,
        updated_at=revision.created_at,
    )
    event = ManagementLifecycleEvent(
        event_id=_management_event_id(
            kind="management-control-baseline-v1",
            revision_id=revision.revision_id,
            profile_id=profile_id,
            reason_code=patch.reason_codes[0],
        ),
        management_authority_id=revision.management_authority_id,
        profile_id=profile_id,
        revision_id=revision.revision_id,
        event_type="proposed",
        reason_code=patch.reason_codes[0],
        created_at=revision.created_at,
    )
    store.transition_management_profile_state(
        profile_id=profile_id,
        authority_id=revision.management_authority_id,
        expected_generation=current.generation,
        state=state,
        event=event,
    )


def _validate_management_activation_rollover(
    *,
    current: AutoRoutingConfig,
    proposal: AutoRoutingConfig,
    revision: ManagementRevision,
    rollover: ManagementActivationRollover,
    store: RoutingStore,
) -> None:
    if current.activation.mode != "active" or proposal.activation.mode != "active":
        raise ConfigConflict("management activation rollover requires active mode")
    if (
        rollover.predecessor.authority_id != revision.preceding_authority_id
        or rollover.predecessor.config_sha != config_revision(current)
    ):
        raise ConfigConflict("management predecessor activation receipt changed")
    persisted_predecessor = store.read_matching_activation_receipt(
        authority_id=rollover.predecessor.authority_id,
        config_sha=rollover.predecessor.config_sha,
        adapter_capability_sha=rollover.predecessor.adapter_capability_sha,
        inventory_contract_sha=rollover.predecessor.inventory_contract_sha,
        inventory_revision=rollover.predecessor.inventory_revision,
    )
    if persisted_predecessor != rollover.predecessor:
        raise ConfigConflict("management predecessor activation receipt changed")
    resulting_authority_id = compute_authority_revision(proposal)
    expected_document = authority_document(proposal)
    if (
        rollover.receipt.authority_id != resulting_authority_id
        or rollover.receipt.config_sha != config_revision(proposal)
        or rollover.receipt.adapter_capability_sha
        != rollover.predecessor.adapter_capability_sha
        or rollover.receipt.inventory_revision != revision.inventory_revision
        or rollover.receipt.inventory_contract_sha != revision.inventory_fingerprint
        or rollover.baseline.authority_id != resulting_authority_id
        or dict(rollover.authority_document) != expected_document
    ):
        raise ConfigConflict("management activation rollover does not match revision")
    snapshot = store.read_inventory_snapshot(revision.inventory_revision)
    if snapshot is None or snapshot.checksum != revision.inventory_fingerprint:
        raise ConfigConflict("management activation inventory changed")


def _rollback_management_activation_rollover(
    *,
    store: RoutingStore,
    rollover: ManagementActivationRollover,
    remove_receipt: bool,
    remove_authority: bool,
    remove_baseline: bool,
    remove_active_pointer: bool,
) -> None:
    if remove_receipt:
        store.rollback_activation_receipt(rollover.receipt)
    if remove_authority or remove_baseline or remove_active_pointer:
        store.rollback_authority_and_baseline(
            authority_id=rollover.receipt.authority_id,
            baseline_revision_id=rollover.baseline.revision_id,
            remove_authority=remove_authority,
            remove_baseline=remove_baseline,
            remove_active_pointer=remove_active_pointer,
        )


def _record_authority_changed_hold(
    *,
    current: AutoRoutingConfig,
    revision: ManagementRevision,
    store: RoutingStore,
) -> None:
    observed_authority_id = compute_authority_revision(current)
    if observed_authority_id == revision.resulting_authority_id:
        return
    patches = tuple(
        ManagementPatch(
            profile_id=patch.profile_id,
            before_runtime_ids=patch.after_runtime_ids,
            after_runtime_ids=_management_runtime_ids(
                current,
                patch.profile_id,
                patch.after_runtime_ids,
            ),
            reason_codes=("authority_changed",),
        )
        for patch in revision.patches
    )
    seed = {
        "kind": "management-authority-changed-v1",
        "parent_revision_id": revision.revision_id,
        "preceding_authority_id": revision.resulting_authority_id,
        "resulting_authority_id": observed_authority_id,
        "management_epoch": revision.management_epoch + 1,
        "patches": [patch.model_dump(mode="json") for patch in patches],
    }
    recovery = ManagementRevision(
        revision_id=_management_hash(seed),
        preceding_authority_id=revision.resulting_authority_id,
        resulting_authority_id=observed_authority_id,
        management_authority_id=revision.management_authority_id,
        parent_revision_id=revision.revision_id,
        ranking_pack=revision.ranking_pack,
        inventory_revision=revision.inventory_revision,
        inventory_fingerprint=revision.inventory_fingerprint,
        management_epoch=revision.management_epoch + 1,
        action="recovery",
        patches=patches,
        runtime_scores=(),
        created_at=revision.created_at,
    )
    with store.write_txn():
        store.publish_management_revision(recovery)
        for patch in patches:
            current_state = store.read_management_profile_state(
                recovery.management_authority_id,
                patch.profile_id,
                current_authority_id=revision.preceding_authority_id,
            )
            state = ManagementProfileState(
                management_authority_id=recovery.management_authority_id,
                profile_id=patch.profile_id,
                authority_id=recovery.resulting_authority_id,
                active_revision_id=recovery.revision_id,
                management_epoch=recovery.management_epoch,
                experiment_phase="recovery_required",
                rejection_count=current_state.rejection_count,
                generation=current_state.generation,
                updated_at=recovery.created_at,
            )
            event = ManagementLifecycleEvent(
                event_id=_management_event_id(
                    kind="management-authority-changed-hold-v1",
                    revision_id=recovery.revision_id,
                    profile_id=patch.profile_id,
                    reason_code="authority_changed",
                ),
                management_authority_id=recovery.management_authority_id,
                profile_id=patch.profile_id,
                revision_id=None,
                event_type="hold",
                reason_code="authority_changed",
                created_at=recovery.created_at,
            )
            store.transition_management_profile_state(
                profile_id=patch.profile_id,
                authority_id=recovery.management_authority_id,
                expected_generation=current_state.generation,
                state=state,
                event=event,
            )


def _freeze_management_recovery(
    *,
    revision: ManagementRevision,
    store: RoutingStore,
) -> None:
    for patch in revision.patches:
        try:
            current = store.read_management_profile_state(
                revision.management_authority_id,
                patch.profile_id,
                current_authority_id=revision.preceding_authority_id,
            )
            state = ManagementProfileState(
                management_authority_id=revision.management_authority_id,
                profile_id=patch.profile_id,
                authority_id=revision.resulting_authority_id,
                active_revision_id=revision.revision_id,
                management_epoch=revision.management_epoch,
                experiment_phase="recovery_required",
                rejection_count=current.rejection_count,
                generation=current.generation,
                updated_at=revision.created_at,
            )
            event = ManagementLifecycleEvent(
                event_id=_management_event_id(
                    kind="management-config-recovery-required-v1",
                    revision_id=revision.revision_id,
                    profile_id=patch.profile_id,
                    reason_code="config_recovery_failed",
                ),
                management_authority_id=revision.management_authority_id,
                profile_id=patch.profile_id,
                revision_id=None,
                event_type="hold",
                reason_code="config_recovery_failed",
                created_at=revision.created_at,
            )
            store.transition_management_profile_state(
                profile_id=patch.profile_id,
                authority_id=revision.management_authority_id,
                expected_generation=current.generation,
                state=state,
                event=event,
            )
        except BaseException:
            # Global freeze is the final fail-closed boundary even if the
            # narrower profile recovery record cannot be advanced.
            pass
    control = store.read_management_control(revision.management_authority_id)
    profile_id = revision.patches[0].profile_id
    event = ManagementLifecycleEvent(
        event_id=_management_event_id(
            kind="management-global-recovery-freeze-v1",
            revision_id=revision.revision_id,
            profile_id=profile_id,
            reason_code="config_recovery_failed",
        ),
        management_authority_id=revision.management_authority_id,
        profile_id=profile_id,
        revision_id=None,
        event_type="frozen",
        reason_code="config_recovery_failed",
        created_at=revision.created_at,
    )
    store.transition_management_control(
        control=ManagementControl(
            management_authority_id=revision.management_authority_id,
            frozen=True,
            changes_today=control.changes_today,
            cron_job_id=control.cron_job_id,
            generation=control.generation,
            updated_at=revision.created_at,
        ),
        expected_generation=control.generation,
        event=event,
    )


def apply_management_config_revision(
    *,
    proposal: AutoRoutingConfig,
    revision: ManagementRevision,
    expected_authority_id: str,
    admission_utc_day: str,
    store: RoutingStore,
    config_path: Path,
    expected_control_generation: int | None = None,
    control_revision: ManagementRevision | None = None,
    activation_rollover: ManagementActivationRollover | None = None,
    defer_profile_state_commit: bool = False,
    lifecycle_finalization: ManagementLifecycleFinalization | None = None,
    precommit_check: Callable[[AutoRoutingConfig], str | None] | None = None,
    mutation_guard: Callable[[AutoRoutingConfig], Any] | None = None,
) -> ManagementRevisionResult:
    """Authorize and apply one management revision under config and store locks."""
    if defer_profile_state_commit and revision.action not in {"promote", "rollback"}:
        raise ValueError(
            "deferred management state commit is only valid for lifecycle revisions"
        )
    if (lifecycle_finalization is not None) != defer_profile_state_commit:
        raise ValueError(
            "lifecycle finalization is required exactly for deferred state commit"
        )
    if lifecycle_finalization is not None and (
        lifecycle_finalization.revision_id != revision.revision_id
        or lifecycle_finalization.receipt_id
        != _management_receipt_id(revision.revision_id)
    ):
        raise ValueError("lifecycle finalization does not bind the revision receipt")
    receipt_id = _management_receipt_id(revision.revision_id)
    backup_path = _management_backup_path(Path(config_path), receipt_id)
    with locked_update(
        proposal,
        path=config_path,
        allow_active=True,
        mutation_guard=mutation_guard,
    ) as update:
        current = update.current_config()
        if update.mutation_guard_reason is not None:
            return ManagementRevisionResult(
                False,
                update.mutation_guard_reason,
                None,
            )
        current_authority_id = compute_authority_revision(current)
        proposal_authority_id = compute_authority_revision(proposal)
        if not hmac.compare_digest(
            expected_authority_id,
            revision.preceding_authority_id,
        ) or not hmac.compare_digest(
            proposal_authority_id,
            revision.resulting_authority_id,
        ):
            return ManagementRevisionResult(
                False,
                "revision_authority_mismatch",
                None,
            )
        existing_receipt = store.read_management_receipt(receipt_id)
        if existing_receipt is not None:
            existing_revision = store.read_management_revision(
                existing_receipt.revision_id
            )
            if existing_revision != revision:
                return ManagementRevisionResult(
                    False,
                    "revision_authority_mismatch",
                    None,
                )
            reason_code = (
                "already_committed"
                if existing_receipt.phase == "committed"
                else "management_recovery_required"
            )
            return ManagementRevisionResult(
                False,
                reason_code,
                revision.revision_id,
            )
        if not hmac.compare_digest(
            current_authority_id,
            expected_authority_id,
        ):
            return ManagementRevisionResult(False, "authority_changed", None)
        receipt = ManagementConfigReceipt(
            receipt_id=receipt_id,
            revision_id=revision.revision_id,
            phase="prepared",
            preceding_authority_id=revision.preceding_authority_id,
            resulting_authority_id=revision.resulting_authority_id,
            backup_checksum=hashlib.sha256(update.preview.before_bytes).hexdigest(),
            created_at=revision.created_at,
            updated_at=revision.created_at,
        )
        authorization_reason: str | None = None
        backup_created = False
        rollover_receipt_created = False
        rollover_authority_created = False
        rollover_baseline_created = False
        rollover_pointer_created = False
        try:
            with store.write_txn() as transaction:
                control = store.read_management_control(
                    revision.management_authority_id,
                    connection=transaction,
                )
                if control.frozen:
                    authorization_reason = "management_frozen"
                elif (
                    expected_control_generation is not None
                    and control.generation != expected_control_generation
                ):
                    authorization_reason = "management_control_changed"
                elif (
                    precommit_check is not None
                    and (precommit_reason := precommit_check(current)) is not None
                ):
                    authorization_reason = precommit_reason
                elif (
                    current.activation.mode == "active" and activation_rollover is None
                ):
                    authorization_reason = "activation_receipt_changed"
                elif (
                    current.activation.mode != "active"
                    and activation_rollover is not None
                ):
                    authorization_reason = "activation_receipt_changed"
                else:
                    if control_revision is not None:
                        if (
                            control_revision.management_authority_id
                            != revision.management_authority_id
                            or control_revision.resulting_authority_id
                            != revision.preceding_authority_id
                            or revision.parent_revision_id
                            != control_revision.revision_id
                        ):
                            raise ConfigConflict(
                                "management control baseline does not precede revision"
                            )
                        _materialize_management_control_revision(
                            store=store,
                            revision=control_revision,
                            profile_id=revision.patches[0].profile_id,
                        )
                    admitted = store.try_admit_management_revision(
                        profile_id=revision.patches[0].profile_id,
                        utc_day=admission_utc_day,
                        daily_limit=(
                            proposal.autonomous_profile_management.daily_change_limit
                        ),
                        revision=revision,
                    )
                    if not admitted:
                        authorization_reason = "daily_cap_reached"
                    else:
                        update.create_backup(backup_path)
                        backup_created = True
                        store.record_management_receipt(receipt)
                        if activation_rollover is not None:
                            _validate_management_activation_rollover(
                                current=current,
                                proposal=proposal,
                                revision=revision,
                                rollover=activation_rollover,
                                store=store,
                            )
                            rollover_receipt_created = (
                                store.read_activation_receipt(
                                    activation_rollover.receipt.receipt_id
                                )
                                is None
                            )
                            rollover_authority_created = (
                                store.read_authority_revision(
                                    activation_rollover.receipt.authority_id
                                )
                                is None
                            )
                            rollover_baseline_created = (
                                store.read_revision(
                                    activation_rollover.baseline.revision_id
                                )
                                is None
                            )
                            rollover_pointer_created = (
                                store.read_active_revision(
                                    activation_rollover.receipt.authority_id
                                )
                                is None
                            )
                            store.publish_authority_and_baseline(
                                authority_id=activation_rollover.receipt.authority_id,
                                document=activation_rollover.authority_document,
                                baseline=activation_rollover.baseline,
                            )
                            store.write_activation_receipt(activation_rollover.receipt)
        except BaseException as original_error:
            if backup_created:
                try:
                    backup_path.unlink(missing_ok=True)
                except BaseException as cleanup_error:
                    raise ConfigRollbackError(
                        backup_path,
                        original_error,
                    ) from cleanup_error
            return ManagementRevisionResult(
                False,
                "config_restored_after_store_failure",
                revision.revision_id,
            )
        if authorization_reason is not None:
            return ManagementRevisionResult(
                False,
                authorization_reason,
                None,
            )

        replace_completed = False
        try:
            update.replace()
            replace_completed = True
            with store.write_txn():
                replaced = _transition_management_receipt(
                    store,
                    receipt,
                    "config_replaced",
                )
                if not defer_profile_state_commit:
                    _commit_management_revision_state(store, revision)
                else:
                    assert lifecycle_finalization is not None
                    store.record_management_lifecycle_finalization(
                        lifecycle_finalization
                    )
                _transition_management_receipt(store, replaced, "committed")
        except BaseException as original_error:
            try:
                update.restore(backup_path)
            except BaseException as recovery_error:
                try:
                    _mark_management_receipt_recovery_required(store, receipt)
                finally:
                    _freeze_management_recovery(revision=revision, store=store)
                raise ConfigRollbackError(
                    backup_path, original_error
                ) from recovery_error
            try:
                if activation_rollover is not None:
                    _rollback_management_activation_rollover(
                        store=store,
                        rollover=activation_rollover,
                        remove_receipt=rollover_receipt_created,
                        remove_authority=rollover_authority_created,
                        remove_baseline=rollover_baseline_created,
                        remove_active_pointer=rollover_pointer_created,
                    )
                _mark_management_receipt_recovery_required(store, receipt)
            except BaseException as recovery_error:
                _freeze_management_recovery(revision=revision, store=store)
                raise ConfigRollbackError(
                    backup_path, original_error
                ) from recovery_error
            reason = (
                "config_restored_after_store_failure"
                if replace_completed
                else "config_restored_after_replace_failure"
            )
            return ManagementRevisionResult(False, reason, revision.revision_id)

        return ManagementRevisionResult(
            True,
            "revision_applied",
            revision.revision_id,
        )


def recover_management_config_revision(
    *,
    receipt: ManagementConfigReceipt,
    store: RoutingStore,
    config_path: Path,
    precommit_check: (
        Callable[[AutoRoutingConfig, ManagementConfigReceipt], str | None] | None
    ) = None,
) -> ManagementRevisionResult:
    """Recover one interrupted receipt without overwriting newer authority."""
    stored_receipt = store.read_management_receipt(receipt.receipt_id)
    if stored_receipt is None:
        raise ConfigConflict("management recovery receipt is unavailable")
    revision = store.read_management_revision(stored_receipt.revision_id)
    if revision is None:
        raise ConfigConflict("management recovery revision is unavailable")
    logical_path = Path(config_path).expanduser().absolute()
    backup_path = _management_backup_path(
        logical_path,
        stored_receipt.receipt_id,
    )
    try:
        initial_document = fast_safe_load(logical_path.read_bytes())
        if not isinstance(initial_document, Mapping):
            raise ValueError("config root is not a mapping")
        initial = parse_config(initial_document)
    except Exception as error:
        _mark_management_receipt_recovery_required(store, stored_receipt)
        _freeze_management_recovery(revision=revision, store=store)
        raise ConfigConflict("management recovery config is invalid") from error

    with locked_update(
        initial,
        path=logical_path,
        allow_active=True,
    ) as update:
        current = update.current_config()
        if precommit_check is not None:
            reason = precommit_check(current, stored_receipt)
            if reason:
                raise ConfigConflict(reason)
        locked_receipt = store.read_management_receipt(stored_receipt.receipt_id)
        if locked_receipt is None:
            raise ConfigConflict("management recovery receipt is unavailable")
        if (
            locked_receipt.phase == "recovery_required"
            and _management_recovery_is_complete(
                receipt=locked_receipt,
                revision=revision,
                store=store,
            )
        ):
            return ManagementRevisionResult(
                False,
                "already_recovered",
                revision.revision_id,
            )
        stored_receipt = locked_receipt
        current_authority_id = compute_authority_revision(current)
        if stored_receipt.phase == "committed":
            if hmac.compare_digest(
                current_authority_id,
                stored_receipt.resulting_authority_id,
            ):
                return ManagementRevisionResult(
                    False,
                    "already_committed",
                    revision.revision_id,
                )
            _record_authority_changed_hold(
                current=current,
                revision=revision,
                store=store,
            )
            return ManagementRevisionResult(False, "authority_changed", None)

        if current_authority_id not in {
            stored_receipt.preceding_authority_id,
            stored_receipt.resulting_authority_id,
        }:
            _mark_management_receipt_recovery_required(store, stored_receipt)
            _record_authority_changed_hold(
                current=current,
                revision=revision,
                store=store,
            )
            return ManagementRevisionResult(False, "authority_changed", None)

        recovery_receipt = _claim_management_receipt_recovery(
            store,
            stored_receipt,
        )
        if current_authority_id == stored_receipt.resulting_authority_id:
            try:
                _record_management_restore_started(
                    receipt=recovery_receipt,
                    failed=revision,
                    store=store,
                )
            except BaseException as marker_error:
                try:
                    _freeze_management_recovery(revision=revision, store=store)
                except BaseException:
                    pass
                raise ConfigConflict(
                    "management recovery restore marker could not be persisted"
                ) from marker_error
        try:
            before_restore = update.target_path.read_bytes()
            update.restore_exact_backup(
                backup_path,
                expected_backup_sha256=recovery_receipt.backup_checksum,
                expected_current_authority_id=current_authority_id,
                expected_restored_authority_id=(
                    recovery_receipt.preceding_authority_id
                ),
            )
        except BaseException as recovery_error:
            _freeze_management_recovery(revision=revision, store=store)
            raise ConfigRollbackError(
                backup_path,
                ConfigConflict("management config recovery failed"),
            ) from recovery_error
        try:
            _finalize_management_recovery(
                receipt=recovery_receipt,
                failed=revision,
                observed_authority_id=current_authority_id,
                store=store,
            )
        except BaseException as finalization_error:
            _freeze_management_recovery(revision=revision, store=store)
            raise ConfigConflict(
                "management recovery finalization failed; management remains frozen"
            ) from finalization_error
        return ManagementRevisionResult(
            before_restore != update.target_path.read_bytes(),
            "recovered",
            revision.revision_id,
        )


def _config_path(path: str | os.PathLike[str] | None) -> Path:
    selected = get_config_path() if path is None else Path(path)
    return selected.expanduser().absolute()


def _pin_config_path(logical_path: Path) -> _PinnedConfigPath:
    target_path = Path(_resolve_target_path(logical_path)).expanduser().absolute()
    return _PinnedConfigPath(
        logical_path=logical_path,
        target_path=target_path,
        logical_lexists=os.path.lexists(logical_path),
        logical_is_symlink=logical_path.is_symlink(),
        target_existed=target_path.exists(),
    )


def _resolve_target_path(logical_path: Path) -> Path:
    return logical_path.resolve(strict=False)


def _prepare_preview(
    proposal: AutoRoutingConfig,
    pinned_path: _PinnedConfigPath,
    *,
    allow_active: bool = False,
) -> _PreparedPreview:
    require_readable_config_before_write(pinned_path.target_path)
    before_bytes = (
        pinned_path.target_path.read_bytes() if pinned_path.target_existed else b""
    )
    before_sha256 = hashlib.sha256(before_bytes).hexdigest()

    validated, normalized = _validate_proposal(
        proposal,
        allow_active=allow_active,
    )
    _reject_managed_subtree()
    after_bytes = _render_after_bytes(
        before_bytes,
        source_existed=pinned_path.target_existed,
        normalized_subtree=normalized,
    )
    after_sha256 = hashlib.sha256(after_bytes).hexdigest()
    revision = compute_authority_revision(validated)
    precondition_sha256 = _precondition_sha256(
        pinned_path=pinned_path,
        before_sha256=before_sha256,
        after_sha256=after_sha256,
        normalized_subtree=normalized,
        authority_revision=revision,
    )
    preview = ConfigPreview(
        config_path=pinned_path.logical_path,
        before_bytes=before_bytes,
        after_bytes=after_bytes,
        unified_diff=_unified_diff(pinned_path.logical_path, before_bytes, after_bytes),
        before_sha256=before_sha256,
        after_sha256=after_sha256,
        authority_revision=revision,
        backup_filename_pattern=(
            f"{pinned_path.logical_path.name}.auto-routing."
            f"{BACKUP_TIMESTAMP_FORMAT}.bak"
        ),
        precondition_sha256=precondition_sha256,
    )
    return _PreparedPreview(
        preview=preview,
        proposal=validated,
    )


def _validate_proposal(
    proposal: AutoRoutingConfig,
    *,
    allow_active: bool = False,
) -> tuple[AutoRoutingConfig, dict[str, Any]]:
    if isinstance(proposal, AutoRoutingConfig):
        raw_subtree: Any = config_document(proposal)
    elif isinstance(proposal, Mapping):
        raw_subtree = dict(proposal)
    else:
        raise ConfigError("auto-routing proposal must be a mapping")

    validated = parse_config({
        "plugins": {
            "entries": {
                "auto-routing": raw_subtree,
            }
        }
    })
    if validated.activation.mode == "active" and not allow_active:
        raise ConfigError(
            "active mode may be written only by the guarded activation command"
        )
    normalized = config_document(validated)
    return validated, normalized


def _reject_managed_subtree() -> None:
    conflicts = sorted(
        str(key)
        for key in managed_scope.managed_config_keys()
        if _paths_overlap(str(key), MANAGED_SUBTREE)
    )
    if conflicts:
        raise ManagedConfigError(
            "refusing to change managed auto-routing config keys: "
            + ", ".join(conflicts)
        )


def _paths_overlap(first: str, second: str) -> bool:
    return (
        first == second
        or first.startswith(f"{second}.")
        or second.startswith(f"{first}.")
    )


def _render_after_bytes(
    before_bytes: bytes,
    *,
    source_existed: bool,
    normalized_subtree: dict[str, Any],
) -> bytes:
    with tempfile.TemporaryDirectory(prefix="hermes-auto-routing-preview-") as temp:
        preview_path = Path(temp) / "config.yaml"
        if source_existed:
            preview_path.write_bytes(before_bytes)
        atomic_roundtrip_yaml_update(
            preview_path,
            MANAGED_SUBTREE,
            normalized_subtree,
        )
        return preview_path.read_bytes()


def _unified_diff(config_path: Path, before: bytes, after: bytes) -> str:
    before_text = before.decode("utf-8")
    after_text = after.decode("utf-8")
    return "".join(
        difflib.unified_diff(
            before_text.splitlines(keepends=True),
            after_text.splitlines(keepends=True),
            fromfile=f"{config_path} (before)",
            tofile=f"{config_path} (after)",
        )
    )


def _precondition_sha256(
    *,
    pinned_path: _PinnedConfigPath,
    before_sha256: str,
    after_sha256: str,
    normalized_subtree: dict[str, Any],
    authority_revision: str,
) -> str:
    payload = {
        "command": CANONICAL_APPLY_COMMAND,
        "logical_config_path": _path_identity(pinned_path.logical_path),
        "target_config_path": _path_identity(pinned_path.target_path),
        "logical_lexists": pinned_path.logical_lexists,
        "logical_is_symlink": pinned_path.logical_is_symlink,
        "target_existed": pinned_path.target_existed,
        "before_sha256": before_sha256,
        "after_sha256": after_sha256,
        "proposal": normalized_subtree,
        "authority_revision": authority_revision,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _path_identity(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path.expanduser())))


def _assert_pinned_target(
    pinned_path: _PinnedConfigPath,
    *,
    before_mutation: bool,
    expected_bytes: bytes | None = None,
) -> None:
    try:
        current_target = _resolve_target_path(pinned_path.logical_path)
        current_is_symlink = pinned_path.logical_path.is_symlink()
        current_lexists = os.path.lexists(pinned_path.logical_path)
    except (OSError, RuntimeError) as exc:
        raise ConfigConflict(
            "config target changed while applying the approved preview"
        ) from exc

    if (
        _path_identity(current_target) != _path_identity(pinned_path.target_path)
        or current_is_symlink != pinned_path.logical_is_symlink
    ):
        raise ConfigConflict(
            "config target changed while applying the approved preview"
        )

    if not before_mutation:
        return

    target_exists = pinned_path.target_path.exists()
    if (
        current_lexists != pinned_path.logical_lexists
        or target_exists != pinned_path.target_existed
    ):
        raise ConfigConflict(
            "config target changed while applying the approved preview"
        )
    if target_exists and expected_bytes is not None:
        try:
            current_bytes = pinned_path.target_path.read_bytes()
        except OSError as exc:
            raise ConfigConflict(
                "config target changed while applying the approved preview"
            ) from exc
        if current_bytes != expected_bytes:
            raise ConfigConflict(
                "config target changed while applying the approved preview"
            )


def _hashes_match(expected: str, actual: str) -> bool:
    if not isinstance(expected, str):
        return False
    try:
        return hmac.compare_digest(expected, actual)
    except TypeError:
        return False


def _verify_applied_config(
    config_path: Path,
    proposal: AutoRoutingConfig,
    expected_bytes: bytes,
    expected_sha256: str,
) -> None:
    try:
        actual_bytes = config_path.read_bytes()
        actual_sha256 = hashlib.sha256(actual_bytes).hexdigest()
        if actual_sha256 != expected_sha256 or actual_bytes != expected_bytes:
            raise ConfigVerificationError(
                "on-disk config verification failed: written bytes changed"
            )
        parsed = parse_config(fast_safe_load(actual_bytes))
        if parsed != proposal:
            raise ConfigVerificationError(
                "on-disk config verification failed: parsed authority changed"
            )
    except ConfigVerificationError:
        raise
    except Exception as exc:
        raise ConfigVerificationError(
            "on-disk config verification failed: config could not be parsed"
        ) from exc


def _file_metadata(path: Path) -> _FileMetadata:
    try:
        file_stat = path.stat()
    except OSError:
        return _FileMetadata(mode=None, owner=None)
    owner = None
    if os.name == "posix":
        owner = (file_stat.st_uid, file_stat.st_gid)
    return _FileMetadata(
        mode=stat.S_IMODE(file_stat.st_mode),
        owner=owner,
    )


def _create_backup(backup_path: Path, content: bytes) -> None:
    if os.path.lexists(backup_path):
        raise FileExistsError(f"refusing to overwrite existing backup {backup_path}")
    _atomic_write_bytes(backup_path, content, mode=0o600, owner=None)


def _restore_original(
    target_path: Path,
    backup_path: Path,
    *,
    source_existed: bool,
    metadata: _FileMetadata,
) -> None:
    if not source_existed:
        target_path.unlink(missing_ok=True)
        _fsync_directory(target_path.parent)
        return
    _atomic_write_bytes(
        target_path,
        backup_path.read_bytes(),
        mode=metadata.mode,
        owner=metadata.owner,
    )


def _replacement_observed_after_error(
    pinned_path: _PinnedConfigPath,
    before_bytes: bytes,
    after_bytes: bytes,
    mutation_state: _MutationState,
) -> bool:
    if mutation_state.target_replaced:
        return True
    if not mutation_state.replace_attempted:
        return False

    try:
        current_bytes = pinned_path.target_path.read_bytes()
    except FileNotFoundError:
        target_exists = False
        current_bytes = None
    except OSError as exc:
        raise ConfigConflict(
            "config target state is indeterminate after failed replacement; "
            "recovery was not attempted"
        ) from exc
    else:
        target_exists = True

    original_observed = (
        pinned_path.target_existed and target_exists and current_bytes == before_bytes
    ) or (not pinned_path.target_existed and not target_exists)
    replacement_observed = target_exists and current_bytes == after_bytes
    if original_observed == replacement_observed:
        raise ConfigConflict(
            "config target state is indeterminate after failed replacement; "
            "recovery was not attempted"
        )
    return replacement_observed


def _atomic_write_bytes(
    path: Path,
    content: bytes,
    *,
    mode: int | None,
    owner: tuple[int, int] | None,
    mutation_state: _MutationState | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.auto-routing-",
        suffix=".tmp",
    )
    descriptor_owned = True
    try:
        if mode is not None and hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        stream = os.fdopen(fd, "wb")
        descriptor_owned = False
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if mutation_state is not None:
            mutation_state.replace_attempted = True
        os.replace(temporary, path)
        _fsync_directory(path.parent)
        if mutation_state is not None:
            mutation_state.target_replaced = True
        real_path = path
        if mode is not None:
            try:
                os.chmod(real_path, mode)
            except OSError:
                pass
        if owner is not None and hasattr(os, "chown"):
            try:
                os.chown(real_path, owner[0], owner[1])
            except OSError:
                pass
    except BaseException:
        if descriptor_owned:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _exclusive_config_locks(*config_paths: Path) -> Iterator[None]:
    lock_paths_by_identity: dict[str, Path] = {}
    for config_path in config_paths:
        lock_path = config_path.with_name(f"{config_path.name}.auto-routing.lock")
        lock_paths_by_identity.setdefault(_path_identity(lock_path), lock_path)
    ordered_lock_paths = [
        lock_paths_by_identity[identity] for identity in sorted(lock_paths_by_identity)
    ]

    thread_locks: list[threading.Lock] = []
    with _thread_locks_guard:
        for lock_path in ordered_lock_paths:
            lock_key = _path_identity(lock_path)
            thread_locks.append(_thread_locks.setdefault(lock_key, threading.Lock()))

    with ExitStack() as stack:
        for thread_lock in thread_locks:
            stack.enter_context(thread_lock)
        for lock_path in ordered_lock_paths:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = stack.enter_context(lock_path.open("a+b"))
            _ensure_windows_lock_byte(lock_file)
            _lock_file(lock_file)
            stack.callback(_unlock_file, lock_file)
        yield


def _ensure_windows_lock_byte(lock_file: Any) -> None:
    if os.name != "nt":
        return
    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write(b"\0")
        lock_file.flush()
        os.fsync(lock_file.fileno())
    lock_file.seek(0)


def _lock_file(lock_file: Any) -> None:
    if os.name == "nt":
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
    else:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)


def _unlock_file(lock_file: Any) -> None:
    if os.name == "nt":
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


__all__ = [
    "AppliedConfig",
    "BACKUP_TIMESTAMP_FORMAT",
    "CANONICAL_APPLY_COMMAND",
    "ConfigConflict",
    "ConfigIOError",
    "ConfigPreview",
    "ConfigRollbackError",
    "ConfigVerificationError",
    "LockedConfigUpdate",
    "ManagedConfigError",
    "apply_update",
    "locked_update",
    "profile_config_lock",
    "preview_update",
]
