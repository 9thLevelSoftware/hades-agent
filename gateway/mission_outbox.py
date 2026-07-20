"""Durable mission/workflow outbox materialization.

The store gives mission-linked and ordinary workflow nodes the same idempotent
materialization surface. This module is deliberately storage-only: claiming,
status transitions, and durable records are reusable by a later dispatcher,
while platform I/O remains outside this boundary.
All durable state is kept in the profile-local :class:`SessionDB`.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Optional

from agent.operation_journal import OperationJournal
from hades_state import SessionDB


OUTBOX_STATUSES = frozenset(
    {
        "pending_approval",
        "scheduled",
        "claimed",
        "delivered",
        "cancelled",
        "failed",
        "unknown",
    }
)

_INITIAL_STATUSES = frozenset({"pending_approval", "scheduled"})
_TERMINAL_STATUSES = frozenset({"delivered", "cancelled", "failed", "unknown"})
_MISSING = object()

# Keys that must never be copied into a durable result/preview.  The outbox
# content itself is the payload that a later delivery layer will need; result records
# are deliberately bounded metadata and must not become a secret-bearing log.
_SECRET_KEY_RE = re.compile(
    r"(?:secret|token|password|passwd|credential|apikey|accesskey|privatekey|"
    r"authorization|cookie|sessionkey)",
    re.IGNORECASE,
)
_SECRET_VALUE_KEYS = frozenset(
    {
        "message",
        "content",
        "prompt",
        "body",
        "text",
        "payload",
        # Raw adapter/process output is terminal metadata, not durable
        # delivery evidence. Redact these keys recursively so nested result
        # payloads cannot turn stdout/stderr/error into a secret-bearing log.
        "stdout",
        "stderr",
        "error",
    }
)
_RAW_OUTPUT_KEY_PARTS = (
    "stdout",
    "stderr",
    "error",
    "traceback",
    "exception",
    "output",
)
_SECRET_TEXT_RE = re.compile(
    r"(?:bearer\s+|(?:token|secret|password|passwd|api[_-]?key)\s*[:=]\s*)"
    r"[^\s,;]+|sk-[A-Za-z0-9_-]+",
    re.IGNORECASE,
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


_PLATFORM_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def normalize_platform_token(value: Any) -> str | None:
    """Return a safe, case-folded platform token without static enum lookup."""
    if not isinstance(value, str):
        return None
    token = value.strip().casefold()
    if not _PLATFORM_TOKEN_RE.fullmatch(token):
        return None
    return token


def _canonical_destination(platform: Any, target: Any) -> str | None:
    """Return the stable platform-qualified authority destination."""
    normalized_platform = normalize_platform_token(platform)
    if normalized_platform is None or not isinstance(target, str):
        return None
    normalized_target = target.strip()
    if not normalized_target:
        return None
    return f"{normalized_platform}:{normalized_target}"


def _canonical_authority_destination(value: Any, platform: Any) -> str | None:
    """Normalize an authority entry without importing gateway configuration.

    A bare entry is scoped to the current platform for compatibility with
    older mission records.  A qualified entry is treated as explicit platform
    scope when its normalized prefix is the current platform; this keeps
    dynamic platforms (for example ``irc:42``) working without consulting a
    static platform enum.  A different prefix remains a bare target, which is
    important for legacy targets that themselves contain a colon (such as
    ``chat:7``).
    """
    if not isinstance(value, str) or not value.strip():
        return None
    current_platform = normalize_platform_token(platform)
    if current_platform is None:
        return None
    raw = value.strip()
    prefix, separator, remainder = raw.partition(":")
    normalized_prefix = normalize_platform_token(prefix)
    if (
        separator
        and normalized_prefix == current_platform
        and remainder.strip()
    ):
        return _canonical_destination(normalized_prefix, remainder)
    return _canonical_destination(current_platform, raw)


def _authority_allows_destination(
    authority: dict[str, Any], *, platform: str, target: str
) -> bool:
    allowed_targets = authority.get("message_targets")
    if not isinstance(allowed_targets, list):
        return False
    expected = _canonical_destination(platform, target)
    if expected is None:
        return False
    return any(
        _canonical_authority_destination(value, platform) == expected
        for value in allowed_targets
    )


def _content_hash(content: Any) -> str:
    return hashlib.sha256(_canonical_json(content).encode("utf-8")).hexdigest()


def _safe_capability_flag(value: Any) -> bool:
    """Coerce a capability declaration to a strict, fail-closed bool.

    Only the literal ``True`` is trusted. ``bool(value)`` would treat a
    malformed declaration like the string ``"false"`` as truthy — Python
    coerces any non-empty string to ``True`` — which is the opposite of
    what a config/plugin author almost certainly meant and silently turns
    "fail closed" into "fail open" for exactly the ambiguous-declaration
    case this contract exists to guard against.
    """
    return value is True


def delivery_capabilities(adapter: Any = None) -> dict[str, bool]:
    """Return the adapter's replay/reconciliation contract.

    Capability lookup is deliberately structural so plugin adapters do not
    need to inherit a gateway class just to participate. Missing or malformed
    declarations fail closed: an ambiguous transport outcome is not replayed.

    The lookup itself is two-tier per capability: the current attribute
    name (``supports_idempotent_delivery`` / ``supports_delivery_reconciliation``)
    is tried first; only when it is genuinely absent does the legacy name
    (``delivery_is_idempotent`` / ``delivery_is_reconcilable``) apply. A
    malformed value on the current attribute name (present but not
    ``True``) is a declaration, not an absence — it does not fall through
    to the legacy name, it fails closed directly.
    """
    idempotent = getattr(adapter, "supports_idempotent_delivery", _MISSING)
    if idempotent is _MISSING:
        idempotent = getattr(adapter, "delivery_is_idempotent", False)
    reconcilable = getattr(adapter, "supports_delivery_reconciliation", _MISSING)
    if reconcilable is _MISSING:
        reconcilable = getattr(adapter, "delivery_is_reconcilable", False)
    return {
        "idempotent": _safe_capability_flag(idempotent),
        "reconcilable": _safe_capability_flag(reconcilable),
    }


def _redact_text(value: str) -> str:
    return _SECRET_TEXT_RE.sub("[REDACTED]", value)


def _redact_value(value: Any, *, key: Optional[str] = None) -> Any:
    """Return a JSON-safe redacted copy for result and preview metadata."""
    if key:
        normalized_key = re.sub(r"[^a-z0-9]", "", key.casefold())
        if (
            normalized_key in _SECRET_VALUE_KEYS
            or any(part in normalized_key for part in _RAW_OUTPUT_KEY_PARTS)
            or _SECRET_KEY_RE.search(normalized_key)
        ):
            return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): _redact_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value))


class MissionOutboxStore:
    """Storage API shared by mission-linked and ordinary workflow outboxes.

    The materialization identity is exactly ``(execution_id, node_id)``.
    IDs are derived from that identity, so callers do not have to coordinate
    random identifiers across retries or process restarts.  Every method is
    storage-only and delegates writes to ``SessionDB``'s transaction helpers.
    """

    statuses = OUTBOX_STATUSES

    def __init__(self, db: SessionDB):
        if not isinstance(db, SessionDB):
            raise TypeError("db must be a SessionDB")
        self.db = db

    @staticmethod
    def _validate_identity(execution_id: str, node_id: str) -> None:
        for name, value in (("execution_id", execution_id), ("node_id", node_id)):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")

    @staticmethod
    def _stable_ids(execution_id: str, node_id: str) -> tuple[str, str]:
        return SessionDB.derive_outbox_ids(execution_id, node_id)

    def materialize(
        self,
        *,
        execution_id: str,
        node_id: str,
        platform: str,
        target: str,
        content: Any,
        mission_id: Optional[str] = None,
        requires_approval: bool = False,
        status: Optional[str] = None,
        not_before: int = 0,
        preview: Any = _MISSING,
        approval: Any = None,
        outbox_id: Optional[str] = None,
        delivery_id: Optional[str] = None,
        transaction_id: Optional[str] = None,
        operation_id: Optional[str] = None,
        content_hash: Optional[str] = None,
        adapter: Any = None,
        idempotent: Optional[bool] = None,
        reconcilable: Optional[bool] = None,
        storage_only: bool = True,
        requeue_cancelled: bool = False,
        requeue_terminal: bool = False,
    ) -> SessionDB.OutboxRecord:
        """Materialize one outbox row without invoking a delivery adapter.

        ``storage_only`` is explicit documentation of this API's boundary; it
        is accepted for callers that pass a preview flag and intentionally has
        no side effect beyond persistence.  Mission rows also get an
        ``agent_operations`` row and an effect transaction. Ordinary workflow
        rows never create an effect transaction.
        """
        del storage_only  # the service has no external/release side effect
        self._validate_identity(execution_id, node_id)
        for name, value in (("platform", platform), ("target", target)):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        normalized_platform = normalize_platform_token(platform)
        if normalized_platform is None:
            raise ValueError("platform must be a safe non-blank token")
        platform = normalized_platform
        target = target.strip()
        if not target:
            raise ValueError("platform and target must be non-empty after normalization")
        if not isinstance(not_before, int) or isinstance(not_before, bool):
            raise ValueError("not_before must be an int")

        computed_hash = _content_hash(content)
        if content_hash is None:
            content_hash = computed_hash
        if content_hash != computed_hash:
            raise ValueError("content_hash does not match content")
        capability_explicit = (
            adapter is not None or idempotent is not None or reconcilable is not None
        )
        capabilities = delivery_capabilities(adapter)
        if idempotent is None:
            idempotent = capabilities["idempotent"]
        if reconcilable is None:
            reconcilable = capabilities["reconcilable"]
        if not isinstance(idempotent, bool) or not isinstance(reconcilable, bool):
            raise ValueError("idempotent and reconcilable must be bools")
        resolved_idempotent = idempotent
        resolved_reconcilable = reconcilable

        initial_status = status if status is not None else (
            "pending_approval" if requires_approval else "scheduled"
        )
        if initial_status not in _INITIAL_STATUSES:
            raise ValueError(
                "materialization status must be 'pending_approval' or 'scheduled'"
            )

        if outbox_id is not None:
            raise ValueError("outbox_id is derived from execution_id and node_id")
        if delivery_id is not None:
            raise ValueError("delivery_id is derived from execution_id and node_id")
        outbox_id, delivery_id = self._stable_ids(execution_id, node_id)

        if transaction_id is not None or operation_id is not None:
            raise ValueError(
                "transaction_id and operation_id are derived from mission outbox identity"
            )

        safe_preview = (
            _redact_value(preview)
            if preview is not _MISSING and preview is not None
            else None
        )
        if mission_id is not None:
            if not isinstance(mission_id, str) or not mission_id:
                raise ValueError("mission_id must be a non-empty string")
            transaction_id = f"{outbox_id}:transaction"
            operation_id = f"{outbox_id}:operation"

        def _materialize(_conn: Any) -> SessionDB.OutboxRecord:
            nonlocal resolved_idempotent, resolved_reconcilable
            existing = self.db.get_outbox_by_identity(execution_id, node_id)
            if existing is not None:
                if existing.mission_id != mission_id:
                    raise ValueError(
                        "existing outbox identity has a different mission_id"
                    )
                if existing.transaction_id != transaction_id:
                    raise ValueError(
                        "existing outbox identity has a different transaction_id"
                    )
                if existing.platform != platform or existing.target != target:
                    raise ValueError(
                        "existing outbox identity has different prepared semantics"
                    )
                if mission_id is not None:
                    assert transaction_id is not None
                    assert operation_id is not None
                    existing_operation = OperationJournal(self.db).get(operation_id)
                    existing_effect = self.db.get_effect_transaction(transaction_id)
                    if existing_effect is None and existing_operation is not None and (
                        existing_operation.state != "pending"
                        or existing_operation.effect_disposition != "none"
                    ):
                        raise ValueError(
                            "missing mission effect: reconciliation required; "
                            "terminal operation cannot be repaired"
                        )
                    if not capability_explicit and existing_effect is not None:
                        existing_semantics = existing_effect.semantics
                        if (
                            isinstance(existing_semantics, dict)
                            and isinstance(existing_semantics.get("idempotent"), bool)
                            and isinstance(existing_semantics.get("reconcilable"), bool)
                        ):
                            resolved_idempotent = existing_semantics["idempotent"]
                            resolved_reconcilable = existing_semantics["reconcilable"]
                if not existing.content_hash:
                    self.db.backfill_outbox_content_hash(existing.outbox_id)
                    existing = self.db.get_outbox_by_id(existing.outbox_id)
                assert existing is not None
                # The persisted hash and preview are the recovery identity.
                # Never let a retry's caller payload become the source of
                # truth, including when the linked effect row is missing and
                # must be recreated.
                if existing.content_hash != computed_hash:
                    raise ValueError(
                        "existing outbox identity has different prepared semantics"
                    )
                if (
                    preview is not _MISSING
                    and _canonical_json(existing.preview) != _canonical_json(safe_preview)
                ):
                    raise ValueError(
                        "existing outbox identity has different preview semantics"
                    )
                if mission_id is not None:
                    assert transaction_id is not None
                    assert operation_id is not None
                    self._ensure_effect_transaction(
                        mission_id=mission_id,
                        execution_id=execution_id,
                        node_id=node_id,
                        platform=platform,
                        target=target,
                        content_hash=existing.content_hash,
                        preview=existing.preview,
                        transaction_id=transaction_id,
                        operation_id=operation_id,
                        idempotent=resolved_idempotent,
                        reconcilable=resolved_reconcilable,
                    )
                if requeue_terminal and existing.status in {"cancelled", "failed"}:
                    self._reset_terminal_mission_state(
                        _conn,
                        existing=existing,
                        expected_status=existing.status,
                    )
                    updated = _conn.execute(
                        """
                        UPDATE mission_outbox
                           SET status = ?, lease_owner = NULL,
                               lease_expires_at = NULL, claim_token = NULL,
                               not_before = ?, revision = revision + 1,
                               approval_json = NULL, result_json = NULL,
                               acknowledged_at = NULL, updated_at = ?
                         WHERE outbox_id = ? AND status IN ('cancelled', 'failed')
                        """,
                        (initial_status, min(existing.not_before, not_before), int(time.time()), existing.outbox_id),
                    ).rowcount
                    if updated != 1:
                        raise RuntimeError("terminal outbox requeue CAS lost")
                    refreshed = _conn.execute(
                        "SELECT * FROM mission_outbox WHERE outbox_id = ?",
                        (existing.outbox_id,),
                    ).fetchone()
                    if refreshed is None:
                        raise RuntimeError("terminal outbox disappeared during requeue")
                    existing = self.db._outbox_from_row(refreshed)
                return existing

            if mission_id is not None:
                assert transaction_id is not None
                assert operation_id is not None
                self._ensure_effect_transaction(
                    mission_id=mission_id,
                    execution_id=execution_id,
                    node_id=node_id,
                    platform=platform,
                    target=target,
                    content_hash=content_hash,
                    preview=safe_preview,
                    transaction_id=transaction_id,
                    operation_id=operation_id,
                    idempotent=resolved_idempotent,
                    reconcilable=resolved_reconcilable,
                )

            # Never persist a result during materialization.  The result column
            # is reserved for redacted terminal metadata written by mark_* methods.
            return self.db.create_outbox(
                execution_id=execution_id,
                node_id=node_id,
                platform=platform,
                target=target,
                content=content,
                content_hash=content_hash,
                preview=safe_preview,
                mission_id=mission_id,
                transaction_id=transaction_id,
                not_before=not_before,
                status=initial_status,
                approval=_redact_value(approval) if approval is not None else None,
                result=None,
            )

        return self.db._run_in_write_transaction(_materialize)

    def _reset_terminal_mission_state(
        self,
        conn: Any,
        *,
        existing: SessionDB.OutboxRecord,
        expected_status: str,
    ) -> None:
        """Reset a failed/cancelled mission identity for a fresh attempt."""
        if existing.mission_id is None:
            return
        if not existing.transaction_id:
            raise ValueError("terminal mission outbox is missing transaction_id")
        operation_id = f"{existing.outbox_id}:operation"
        effect = conn.execute(
            "SELECT * FROM effect_transactions WHERE transaction_id = ?",
            (existing.transaction_id,),
        ).fetchone()
        if effect is None:
            raise ValueError("terminal mission effect is missing")
        if (
            effect["operation_id"] != operation_id
            or effect["mission_id"] != existing.mission_id
            or effect["execution_id"] != existing.execution_id
            or effect["step_id"] != existing.node_id
        ):
            raise ValueError("terminal mission effect identity does not match outbox")
        expected_phase = {"failed": "failed", "cancelled": "cancelled"}.get(expected_status)
        reset_phase = effect["phase"]
        allowed_phases = {expected_phase}
        if expected_status == "cancelled":
            # Legacy callers can cancel the outbox before separately settling
            # its effect. Requeue repairs that pre-dispatch cancellation in
            # the same transaction instead of reusing a stale pending row.
            allowed_phases.update({"pending", "previewed"})
        if reset_phase not in allowed_phases:
            raise ValueError(
                f"terminal mission effect phase {reset_phase!r} does not match "
                f"outbox status {expected_status!r}"
            )
        operation = conn.execute(
            "SELECT * FROM agent_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if operation is None:
            raise ValueError("terminal mission operation is missing")
        if operation["state"] not in {"pending", "failed", "cancelled"}:
            raise ValueError(
                "terminal mission operation is not resettable; reconciliation required"
            )
        now = int(time.time())
        reset_effect = conn.execute(
            """UPDATE effect_transactions
                  SET phase = 'pending', authority_json = NULL,
                      result_json = NULL, verification_json = NULL,
                      compensation_json = NULL, updated_at = ?
                WHERE transaction_id = ? AND operation_id = ?
                  AND mission_id = ? AND phase = ?""",
            (
                now,
                existing.transaction_id,
                operation_id,
                existing.mission_id,
                reset_phase,
            ),
        )
        if reset_effect.rowcount != 1:
            raise RuntimeError("terminal mission effect reset CAS lost")
        reset_operation = conn.execute(
            """UPDATE agent_operations
                  SET state = 'pending', effect_disposition = 'none',
                      result_json = NULL, error = NULL, acknowledged_at = NULL,
                      updated_at = ?
                WHERE operation_id = ? AND state IN ('pending', 'failed', 'cancelled')""",
            (now, operation_id),
        )
        if reset_operation.rowcount != 1:
            raise RuntimeError("terminal mission operation reset CAS lost")

    def _ensure_effect_transaction(
        self,
        *,
        mission_id: str,
        execution_id: str,
        node_id: str,
        platform: str,
        target: str,
        content_hash: str,
        preview: Any,
        transaction_id: str,
        operation_id: str,
        idempotent: bool,
        reconcilable: bool,
    ) -> None:
        prepared = {
            "delivery_kind": "outbox",
            "platform": platform,
            "target": target,
            "content_hash": content_hash,
            "execution_id": execution_id,
            "node_id": node_id,
        }
        semantics = {
            "kind": "outbound_delivery",
            "idempotent": idempotent,
            "reconcilable": reconcilable,
        }
        journal = OperationJournal(self.db)
        operation = journal.create(
            operation_id=operation_id,
            kind="mission_outbox",
            destination=f"outbox:{platform}",
            payload_hash=content_hash,
        )
        if (
            operation.kind != "mission_outbox"
            or operation.destination != f"outbox:{platform}"
            or operation.payload_hash != content_hash
        ):
            raise ValueError("operation journal identity does not match effect semantics")
        existing_effect = self.db.get_effect_transaction(transaction_id)
        if existing_effect is None and (
            operation.state != "pending" or operation.effect_disposition != "none"
        ):
            raise ValueError(
                "missing mission effect: reconciliation required; "
                "terminal operation cannot be repaired"
            )
        if existing_effect is not None:
            expected_identity = (
                operation_id,
                mission_id,
                execution_id,
                node_id,
            )
            actual_identity = (
                existing_effect.operation_id,
                existing_effect.mission_id,
                existing_effect.execution_id,
                existing_effect.step_id,
            )
            if actual_identity != expected_identity:
                raise ValueError(
                    "effect transaction identity already belongs to a different mission effect"
                )
            if (
                existing_effect.adapter_id != f"outbox.{platform}"
                or existing_effect.semantics != semantics
                or existing_effect.depends_on != []
                or existing_effect.prepared != prepared
            ):
                raise ValueError(
                    "effect transaction prepared semantics do not match existing effect"
                )
            if _canonical_json(existing_effect.preview) != _canonical_json(preview):
                raise ValueError(
                    "effect transaction preview semantics do not match existing effect"
                )
            return
        # Allocate the mission sequence only inside SessionDB's write
        # transaction so concurrent materializations cannot observe the same
        # next slot.
        try:
            self.db.create_effect_transaction(
                transaction_id=transaction_id,
                operation_id=operation_id,
                mission_id=mission_id,
                execution_id=execution_id,
                step_id=node_id,
                adapter_id=f"outbox.{platform}",
                sequence_no=None,
                semantics=semantics,
                depends_on=[],
                prepared=prepared,
                preview=preview,
                phase="pending",
            )
        except ValueError:
            # A concurrent retry may have won the deterministic transaction
            # id.  Do not turn that successful idempotent materialization into
            # an error; unrelated conflicts remain visible.
            if self.db.get_effect_transaction(transaction_id) is None:
                raise

    def requeue_cancelled(self, **kwargs: Any) -> SessionDB.OutboxRecord:
        """Atomically validate and reactivate one cancelled durable identity.

        Validation, mission effect/link checks, and the cancelled→scheduled
        transition share one SessionDB write transaction. If validation fails,
        the cancelled outbox and its operation/effect rows remain untouched.
        """
        kwargs["requeue_terminal"] = True
        return self.materialize(**kwargs)

    def requeue_terminal(self, **kwargs: Any) -> SessionDB.OutboxRecord:
        """Atomically requeue a failed or cancelled durable identity."""
        kwargs["requeue_terminal"] = True
        return self.materialize(**kwargs)

    def preview(self, **kwargs: Any) -> SessionDB.OutboxRecord:
        """Persist a redacted storage preview without routing or dispatching.

        This is not a dry-run of an adapter: it creates the same durable
        outbox record as :meth:`materialize`, with no delivery side effect.
        """
        kwargs["storage_only"] = True
        return self.materialize(**kwargs)

    materialize_outbox = materialize
    materialize_mission_outbox = materialize

    def get(self, execution_id: str, node_id: str) -> Optional[SessionDB.OutboxRecord]:
        return self.db.get_outbox_by_identity(execution_id, node_id)

    def get_by_id(self, outbox_id: str) -> Optional[SessionDB.OutboxRecord]:
        return self.db.get_outbox_by_id(outbox_id)

    def get_by_delivery_id(self, delivery_id: str) -> Optional[SessionDB.OutboxRecord]:
        return self.db.get_outbox(delivery_id)

    def schedule(self, outbox_id: str, *, expected_status: str = "pending_approval") -> bool:
        return self.db.transition_outbox(
            outbox_id,
            expected_status=expected_status,
            next_status="scheduled",
        )

    def revise(
        self,
        outbox_id: str,
        *,
        content: Any,
        not_before: int = 0,
        expected_revision: Optional[int] = None,
        preview: Any = _MISSING,
    ) -> Optional[SessionDB.OutboxRecord]:
        current = self.db.get_outbox_by_id(outbox_id)
        if current is None:
            return None
        if normalize_platform_token(current.platform) is None:
            raise ValueError("outbox platform must be a safe non-blank token")
        if expected_revision is None:
            expected_revision = current.revision
        revise_kwargs: dict[str, Any] = {
            "expected_revision": expected_revision,
            "content": content,
            "not_before": not_before,
        }
        if preview is not _MISSING:
            revise_kwargs["preview"] = preview
        return self.db.revise_outbox(outbox_id, **revise_kwargs)

    def cancel(
        self,
        outbox_id: str,
        *,
        expected_revision: Optional[int] = None,
        owner_id: Optional[str] = None,
        claim_token: Optional[str] = None,
    ) -> bool:
        if expected_revision is None:
            current = self.db.get_outbox_by_id(outbox_id)
            if current is None:
                return False
            expected_revision = current.revision
        return self.db.cancel_outbox(
            outbox_id,
            expected_revision=expected_revision,
            owner_id=owner_id,
            claim_token=claim_token,
        )

    def set_delivery_capabilities(
        self,
        outbox_id: str,
        *,
        adapter: Any = None,
        idempotent: Optional[bool] = None,
        reconcilable: Optional[bool] = None,
    ) -> bool:
        """Bind the live adapter recovery contract before router delivery.

        Validates the full outbox <-> operation <-> effect graph (P0-2)
        atomically inside the same write transaction as the mutation, so a
        corrupted or mismatched ``prepared_json``, destination, or operation
        identity blocks the capability write rather than being silently
        carried through toward delivery.
        """
        capabilities = delivery_capabilities(adapter)
        if idempotent is not None:
            capabilities["idempotent"] = idempotent
        if reconcilable is not None:
            capabilities["reconcilable"] = reconcilable
        if not all(isinstance(value, bool) for value in capabilities.values()):
            raise ValueError("delivery capabilities must be bools")

        def _update(conn: Any) -> bool:
            row = conn.execute(
                "SELECT * FROM mission_outbox WHERE outbox_id = ?",
                (outbox_id,),
            ).fetchone()
            if row is None or row["mission_id"] is None or not row["transaction_id"]:
                return True
            effect = conn.execute(
                "SELECT * FROM effect_transactions WHERE transaction_id = ?",
                (row["transaction_id"],),
            ).fetchone()
            if effect is None:
                return False
            if not self.db._mission_outbox_effect_identity_matches(conn, row, effect):
                return False
            if effect["phase"] not in {"pending", "previewed"}:
                return False
            try:
                semantics = self.db._effect_parse_json(effect["semantics_json"])
            except ValueError:
                return False
            if not isinstance(semantics, dict) or semantics.get("kind") != "outbound_delivery":
                return False
            semantics["idempotent"] = capabilities["idempotent"]
            semantics["reconcilable"] = capabilities["reconcilable"]
            updated = conn.execute(
                """UPDATE effect_transactions SET semantics_json = ?, updated_at = ?
                    WHERE transaction_id = ? AND phase IN ('pending', 'previewed')""",
                (
                    self.db._canonicalize_outbox_optional_payload(semantics),
                    int(time.time()),
                    row["transaction_id"],
                ),
            )
            return updated.rowcount == 1

        return bool(self.db._run_in_write_transaction(_update))

    def release(
        self,
        outbox_id: str,
        *,
        claim_token: str,
        owner_id: Optional[str] = None,
        next_status: str = "scheduled",
    ) -> bool:
        return self.db.release_outbox(
            outbox_id,
            owner_id=owner_id,
            claim_token=claim_token,
            next_status=next_status,
        )

    def compensate(
        self,
        outbox_id: str,
        *,
        unknown_result: Any = _MISSING,
    ) -> str:
        """Atomically compensate a workflow persistence failure.

        Unclaimed rows are cancelled together with their pre-dispatch effect.
        A claimed row is fenced by its current claim token and becomes
        explicitly unknown because an adapter call may be in flight. CAS or
        identity failures raise rather than being silently ignored.
        """
        if not isinstance(outbox_id, str) or not outbox_id:
            raise ValueError("outbox_id must be a non-empty string")
        if unknown_result is _MISSING:
            unknown_result = {
                "error": "workflow persistence failed after outbox claim",
                "reconciliation_required": True,
            }
        unknown_json = self.db._canonicalize_outbox_optional_payload(unknown_result)

        def _compensate(conn: Any) -> str:
            row = conn.execute(
                "SELECT * FROM mission_outbox WHERE outbox_id = ?",
                (outbox_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"outbox {outbox_id!r} disappeared during compensation")
            status = row["status"]
            if status in {"delivered", "failed", "cancelled", "unknown"}:
                return "terminal"
            effect = None
            if row["mission_id"] is not None:
                if not row["transaction_id"]:
                    raise RuntimeError("mission outbox compensation missing transaction_id")
                effect = conn.execute(
                    "SELECT * FROM effect_transactions WHERE transaction_id = ?",
                    (row["transaction_id"],),
                ).fetchone()
                if effect is None:
                    raise RuntimeError("mission outbox compensation effect identity mismatch")
                if not self.db._mission_outbox_effect_identity_matches(conn, row, effect):
                    raise RuntimeError("mission outbox compensation effect identity mismatch")
            now = int(time.time())
            if status in {"pending", "pending_approval", "scheduled"}:
                if effect is not None:
                    if effect["phase"] not in {"pending", "previewed"}:
                        raise RuntimeError(
                            "unclaimed mission outbox effect crossed the delivery boundary"
                        )
                    settled = conn.execute(
                        """UPDATE effect_transactions
                              SET phase = 'cancelled', updated_at = ?
                            WHERE transaction_id = ? AND mission_id = ?
                              AND phase IN ('pending', 'previewed')""",
                        (now, row["transaction_id"], row["mission_id"]),
                    )
                    if settled.rowcount != 1:
                        raise RuntimeError("unclaimed mission effect cancellation CAS lost")
                cancelled = conn.execute(
                    """UPDATE mission_outbox
                          SET status = 'cancelled', lease_owner = NULL,
                              lease_expires_at = NULL, claim_token = NULL,
                              updated_at = ?
                        WHERE outbox_id = ?
                          AND status IN ('pending', 'pending_approval', 'scheduled')""",
                    (now, outbox_id),
                )
                if cancelled.rowcount != 1:
                    raise RuntimeError("unclaimed outbox cancellation CAS lost")
                return "cancelled"
            if status == "claimed":
                claim_token = row["claim_token"]
                if not isinstance(claim_token, str) or not claim_token:
                    raise RuntimeError("claimed outbox is missing claim_token")
                if effect is not None:
                    if effect["phase"] not in {
                        "pending", "previewed", "committing", "unknown_effect"
                    }:
                        raise RuntimeError("claimed mission outbox effect is already terminal")
                    if effect["phase"] != "unknown_effect":
                        uncertain = conn.execute(
                            """UPDATE effect_transactions
                                  SET phase = 'unknown_effect', compensation_json = ?,
                                      updated_at = ?
                                WHERE transaction_id = ? AND mission_id = ?
                                  AND phase IN ('pending', 'previewed', 'committing')""",
                            (unknown_json, now, row["transaction_id"], row["mission_id"]),
                        )
                        if uncertain.rowcount != 1:
                            raise RuntimeError("claimed mission effect uncertainty CAS lost")
                unknown = conn.execute(
                    """UPDATE mission_outbox
                          SET status = 'unknown', result_json = ?,
                              lease_owner = NULL, lease_expires_at = NULL,
                              claim_token = NULL, updated_at = ?
                        WHERE outbox_id = ? AND status = 'claimed'
                          AND claim_token = ?""",
                    (unknown_json, now, outbox_id, claim_token),
                )
                if unknown.rowcount != 1:
                    raise RuntimeError("claimed outbox uncertainty CAS lost")
                return "unknown"
            raise RuntimeError(f"outbox {outbox_id!r} is not compensatable from {status!r}")

        return self.db._run_in_write_transaction(_compensate)

    def claim(
        self,
        now: Optional[int] = None,
        *,
        owner_id: Optional[str] = None,
        lease_seconds: int = 60,
        limit: Optional[int] = None,
        eligible_outbox_ids: Optional[set[str]] = None,
        require_mission_approval: bool = False,
    ) -> list[SessionDB.OutboxRecord]:
        return self.db.claim_due_outbox(
            int(time.time()) if now is None else now,
            lease_seconds=lease_seconds,
            limit=limit,
            owner_id=owner_id,
            eligible_outbox_ids=eligible_outbox_ids,
            require_mission_approval=require_mission_approval,
        )

    claim_due = claim

    def transition(
        self,
        outbox_id: str,
        *,
        next_status: str,
        expected_status: Optional[str] = None,
        result: Any = None,
        owner_id: Optional[str] = None,
        claim_token: Optional[str] = None,
    ) -> bool:
        if next_status not in OUTBOX_STATUSES:
            raise ValueError(f"invalid outbox status: {next_status!r}")
        if expected_status is None:
            current = self.db.get_outbox_by_id(outbox_id)
            if current is None:
                return False
            expected_status = current.status
        if expected_status == "claimed" and next_status in _TERMINAL_STATUSES:
            if not claim_token:
                return False
        return self.db.transition_outbox(
            outbox_id,
            expected_status=expected_status,
            next_status=next_status,
            result=_redact_value(result),
            owner_id=owner_id,
            claim_token=claim_token,
        )

    def mark_delivered(
        self,
        outbox_id: str,
        *,
        result: Any = None,
        expected_status: str = "claimed",
        owner_id: Optional[str] = None,
        claim_token: Optional[str] = None,
    ) -> bool:
        return self.transition(
            outbox_id,
            expected_status=expected_status,
            next_status="delivered",
            result=result,
            owner_id=owner_id,
            claim_token=claim_token,
        )

    def mark_failed(
        self,
        outbox_id: str,
        *,
        result: Any = None,
        error: Optional[str] = None,
        expected_status: str = "claimed",
        owner_id: Optional[str] = None,
        claim_token: Optional[str] = None,
    ) -> bool:
        payload = result if result is not None else {}
        if error is not None:
            if isinstance(payload, dict):
                payload = {**payload, "error": error}
            elif result is not None:
                payload = {"result": result, "error": error}
            else:
                payload = {"error": error}
        return self.transition(
            outbox_id,
            expected_status=expected_status,
            next_status="failed",
            result=payload,
            owner_id=owner_id,
            claim_token=claim_token,
        )

    def mark_unknown(
        self,
        outbox_id: str,
        *,
        result: Any = None,
        expected_status: str = "claimed",
        owner_id: Optional[str] = None,
        claim_token: Optional[str] = None,
    ) -> bool:
        return self.transition(
            outbox_id,
            expected_status=expected_status,
            next_status="unknown",
            result=result,
            owner_id=owner_id,
            claim_token=claim_token,
        )

    def acknowledge(self, outbox_id: str) -> bool:
        return self.db.acknowledge_outbox(outbox_id)

    acknowledge_terminal = acknowledge
    create = materialize
    create_outbox = materialize
    revise_outbox = revise
    cancel_outbox = cancel
    claim_due_outbox = claim
    release_outbox = release
    transition_outbox = transition
    acknowledge_outbox = acknowledge


# Names used by callers that describe this as a delivery outbox rather than a
# mission outbox.  They intentionally point at the same implementation.
DurableOutboxStore = MissionOutboxStore
OutboxStore = MissionOutboxStore
MissionOutbox = MissionOutboxStore
DeliveryOutbox = MissionOutboxStore
OutboxService = MissionOutboxStore
OutboxRecord = SessionDB.OutboxRecord


def materialize_outbox(db: SessionDB, **kwargs: Any) -> SessionDB.OutboxRecord:
    """Functional convenience wrapper around :class:`MissionOutboxStore`."""
    return MissionOutboxStore(db).materialize(**kwargs)


__all__ = [
    "DeliveryOutbox",
    "DurableOutboxStore",
    "MissionOutbox",
    "MissionOutboxStore",
    "OUTBOX_STATUSES",
    "OutboxRecord",
    "OutboxService",
    "OutboxStore",
    "delivery_capabilities",
    "materialize_outbox",
]
