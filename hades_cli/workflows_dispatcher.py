"""SQLite dispatcher for cheap local workflow nodes."""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.operation_journal import OperationJournal
from gateway.mission_outbox import (
    MissionOutboxStore,
    _authority_allows_destination,
    normalize_platform_token,
)
from hades_constants import get_hades_home
from hades_state import SessionDB, _redact_durable_value
from hades_cli import kanban_db as kb
from hades_cli import missions_db as mdb
from hades_cli import workflows_db as wfdb
from hades_cli.workflows_engine import EngineResult, render_template, run_in_memory_until_waiting
from hades_cli.workflows_prompts import render_agent_prompt, render_prompt_text
from hades_cli.workflows_spec import RESULT_CONTRACT_PRIMITIVES, WorkflowSpec

logger = logging.getLogger(__name__)


@dataclass
class TickReport:
    schedules_admitted: int = 0
    feed_items_admitted: int = 0
    executions_advanced: int = 0
    remaining_queued: int = 0
    remaining_running_or_waiting: int = 0
    processed: int = 0


class _AgentTaskMaterializationError(RuntimeError):
    phase = "agent_task_materialization"

    def __init__(self, node_id: str, message: str):
        super().__init__(message)
        self.node_id = node_id


class _SendMessageMaterializationError(RuntimeError):
    phase = "send_message_materialization"

    def __init__(self, node_id: str, message: str):
        super().__init__(message)
        self.node_id = node_id


# Terminal outbox state is only authoritative when its linked durable effect
# transaction reached the corresponding terminal phase.  In particular,
# ``delivered`` is never inferred from a merely prepared, in-flight, failed,
# cancelled, or unknown effect.
_TERMINAL_OUTBOX_EFFECT_PHASES: dict[str, frozenset[str]] = {
    "delivered": frozenset({"committed"}),
    "failed": frozenset({"failed"}),
    "cancelled": frozenset({"cancelled"}),
    "unknown": frozenset({"unknown_effect"}),
}


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _max_outbox_delay_seconds() -> int:
    from hades_cli.config import load_config

    value = load_config().get("missions", {}).get("outbox", {}).get("max_delay_seconds")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("missions.outbox.max_delay_seconds must be a positive integer")
    return value


def _active_profile_name() -> str:
    home = get_hades_home().expanduser().resolve(strict=False)
    if home.parent.name == "profiles":
        return home.name
    return "default"


def _profile_path_error(
    *,
    workflow_db_path: Path | None,
    state_db_path: Path | None,
    node_id: str,
) -> str | None:
    """Reject explicit databases that are outside the active profile home."""
    active_home = get_hades_home().expanduser().resolve(strict=False)
    configured_profile = os.environ.get("HERMES_PROFILE", "").strip()
    active_profile = _active_profile_name()
    if configured_profile and configured_profile != active_profile:
        return (
            f"send_message node {node_id!r} rejected: HERMES_PROFILE "
            f"{configured_profile!r} conflicts with active profile home {active_home}"
        )
    expected_workflow = (active_home / "workflows.db").resolve(strict=False)
    expected_state = (active_home / "state.db").resolve(strict=False)
    if workflow_db_path is not None:
        actual = Path(workflow_db_path).expanduser().resolve(strict=False)
        if actual != expected_workflow:
            return (
                f"send_message node {node_id!r} rejected: workflow database "
                f"{actual} is outside active profile home {active_home}"
            )
    if state_db_path is not None:
        actual = Path(state_db_path).expanduser().resolve(strict=False)
        if actual != expected_state:
            return (
                f"send_message node {node_id!r} rejected: state database "
                f"{actual} is outside active profile home {active_home}"
            )
    return None


def _claim_next(
    conn: sqlite3.Connection,
    *,
    now: int,
    lease_seconds: int,
) -> tuple[str, str] | None:
    row = conn.execute(
        """
        SELECT execution_id
          FROM workflow_executions
         WHERE status = 'queued'
           AND (claim_lock IS NULL OR claim_expires <= ?)
         ORDER BY created_at, execution_id
         LIMIT 1
        """,
        (now,),
    ).fetchone()
    if row is None:
        return None

    token = secrets.token_hex(16)
    with wfdb.write_txn(conn):
        updated = conn.execute(
            """
            UPDATE workflow_executions
               SET claim_lock = ?, claim_expires = ?, updated_at = ?
             WHERE execution_id = ?
               AND status = 'queued'
               AND (claim_lock IS NULL OR claim_expires <= ?)
            """,
            (token, now + lease_seconds, now, row["execution_id"], now),
        ).rowcount
    if updated != 1:
        return None
    return row["execution_id"], token


def _append_event(
    conn: sqlite3.Connection,
    execution_id: str,
    kind: str,
    payload: dict[str, Any] | None,
    now: int,
) -> None:
    conn.execute(
        """
        INSERT INTO workflow_events (
            execution_id, node_run_id, kind, payload_json, created_at
        ) VALUES (?, NULL, ?, ?, ?)
        """,
        (execution_id, kind, _json_dumps(payload or {}), now),
    )


def _schedule_input(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    spec = wfdb.get_definition(conn, row["workflow_id"], row["version"])
    for trigger in spec.triggers:
        if trigger.type != "schedule":
            continue
        expr = trigger.cron or trigger.schedule or getattr(trigger, "expr", None)
        if row["trigger_id"] is not None:
            if trigger.id == row["trigger_id"]:
                return dict(trigger.input)
        elif expr == row["schedule"]:
            return dict(trigger.input)
    return {}


def _fire_due_schedules(conn: sqlite3.Connection, *, now: int, limit: int) -> int:
    if limit <= 0:
        return 0
    started = 0
    with wfdb.write_txn(conn):
        rows = conn.execute(
            """
            SELECT * FROM workflow_schedules
             WHERE enabled = 1
               AND next_run_at IS NOT NULL
               AND next_run_at <= ?
             ORDER BY next_run_at, id
             LIMIT ?
            """,
            (now, limit),
        ).fetchall()
        for row in rows:
            try:
                wfdb.start_execution(
                    conn,
                    row["workflow_id"],
                    input_data=_schedule_input(conn, row),
                    trigger_type="schedule",
                    trigger_id=row["trigger_id"],
                    version=row["version"],
                    now=now,
                )
            except (KeyError, ValueError):
                # Definition deleted or disabled after the schedule row was
                # registered — drop the stale schedule instead of failing the
                # whole tick forever.
                conn.execute("DELETE FROM workflow_schedules WHERE id = ?", (row["id"],))
                continue
            conn.execute(
                """
                UPDATE workflow_schedules
                   SET next_run_at = ?, updated_at = ?
                 WHERE id = ?
                """,
                (wfdb._next_cron_run(row["schedule"], now), now, row["id"]),
            )
            started += 1
    return started


def _terminal_projection_error(
    row: sqlite3.Row,
    outbox: SessionDB.OutboxRecord,
    *,
    reason: str,
    expected_mission_id: str | None = None,
) -> dict[str, Any]:
    return {
        "message": "terminal outbox identity mismatch; reconciliation required",
        "node": row["node_id"],
        "phase": "outbox_terminal",
        "outbox_id": outbox.outbox_id,
        "outbox_status": outbox.status,
        "mission_id": outbox.mission_id,
        "expected_mission_id": expected_mission_id,
        "reason": "unknown_effect",
        "identity_mismatch": reason,
        "reconciliation_required": True,
    }


def _projected_outbox_result(outbox: SessionDB.OutboxRecord) -> Any:
    """Return terminal metadata without collapsing legitimate falsey values."""
    return _redact_durable_value({} if outbox.result is None else outbox.result)


def _mission_outbox_identity_error(
    state_db: SessionDB,
    outbox: SessionDB.OutboxRecord,
    *,
    expected_mission: mdb.MissionRecord | None,
) -> str | None:
    """Validate the complete durable identity graph before terminal projection."""
    expected_mission_id = expected_mission.mission_id if expected_mission is not None else None
    if outbox.mission_id != expected_mission_id:
        return (
            f"outbox mission {outbox.mission_id!r} does not match "
            f"execution mission {expected_mission_id!r}"
        )
    if outbox.mission_id is None:
        return None
    if outbox.transaction_id != f"{outbox.outbox_id}:transaction":
        return "outbox transaction identity does not match derived mission transaction"
    platform = normalize_platform_token(outbox.platform)
    if platform is None or platform != outbox.platform:
        return "outbox platform is not a canonical safe token"
    expected_operation_id = f"{outbox.outbox_id}:operation"
    operation = OperationJournal(state_db).get(expected_operation_id)
    if operation is None:
        return "linked mission operation journal row is missing"
    if (
        operation.operation_id != expected_operation_id
        or operation.kind != "mission_outbox"
        or operation.destination != f"outbox:{platform}"
        or operation.payload_hash != outbox.content_hash
    ):
        return "linked mission operation journal identity does not match outbox semantics"
    if not outbox.transaction_id:
        return "linked mission effect transaction id is missing"
    effect = state_db.get_effect_transaction(outbox.transaction_id)
    if effect is None:
        return "linked mission effect transaction is missing"
    semantics = effect.semantics
    if (
        not isinstance(semantics, dict)
        or semantics.get("kind") != "outbound_delivery"
        or not isinstance(semantics.get("idempotent"), bool)
        or not isinstance(semantics.get("reconcilable"), bool)
    ):
        return "linked mission effect transaction identity does not match outbox semantics"
    expected_semantics = {
        "kind": "outbound_delivery",
        "idempotent": semantics["idempotent"],
        "reconcilable": semantics["reconcilable"],
    }
    expected_prepared = {
        "delivery_kind": "outbox",
        "platform": platform,
        "target": outbox.target,
        "content_hash": outbox.content_hash,
        "execution_id": outbox.execution_id,
        "node_id": outbox.node_id,
    }
    if (
        effect.transaction_id != outbox.transaction_id
        or effect.operation_id != expected_operation_id
        or effect.mission_id != outbox.mission_id
        or effect.execution_id != outbox.execution_id
        or effect.step_id != outbox.node_id
        or effect.adapter_id != f"outbox.{platform}"
        or effect.semantics != expected_semantics
        or effect.depends_on != []
        or effect.prepared != expected_prepared
        or effect.preview != outbox.preview
    ):
        return "linked mission effect transaction identity does not match outbox semantics"
    required_phases = _TERMINAL_OUTBOX_EFFECT_PHASES.get(outbox.status)
    if required_phases is None or effect.phase not in required_phases:
        expected = sorted(required_phases or ())
        return (
            f"effect phase {effect.phase!r} is incompatible with terminal outbox "
            f"status {outbox.status!r}; expected one of {expected!r}"
        )
    return None


def _block_terminal_projection(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    error: dict[str, Any],
    now: int,
) -> None:
    updated = conn.execute(
        """
        UPDATE workflow_node_runs
           SET status = 'blocked', error = ?, completed_at = ?, wait_until = NULL
         WHERE id = ? AND status = 'waiting'
        """,
        (_json_dumps(error), now, row["id"]),
    ).rowcount
    if not updated:
        return
    try:
        context = json.loads(row["context_json"] or "{}")
    except (TypeError, ValueError):
        context = {"node": {}}
    conn.execute(
        """
        UPDATE workflow_executions
           SET status = 'blocked', context_json = ?,
               claim_lock = NULL, claim_expires = NULL, updated_at = ?
         WHERE execution_id = ? AND status IN ('waiting', 'queued')
        """,
        (
            _json_dumps(_context_with_error(context, error)),
            now,
            row["execution_id"],
        ),
    )
    _append_event(conn, row["execution_id"], "execution_blocked", error, now)


def _record_reconciliation_review(
    conn: sqlite3.Connection,
    mission: mdb.MissionRecord | None,
    *,
    outbox: SessionDB.OutboxRecord,
    detail: dict[str, Any],
    now: int,
    suffix: str,
) -> None:
    if mission is None or mission.profile != _active_profile_name():
        # A profile-mismatched mission is foreign state. Keep the diagnostic in
        # the local workflow projection only; never insert a review row into a
        # mission aggregate owned by another profile.
        return
    try:
        mdb.upsert_review_item(
            conn,
            mission.mission_id,
            review_id=f"workflow-outbox:{outbox.outbox_id}:{suffix}",
            kind="unknown_effect",
            transaction_id=outbox.transaction_id,
            detail=detail,
            status="pending",
            now=now,
        )
    except (KeyError, mdb.MissionStateError, sqlite3.Error):
        # A malformed mission aggregate must not stop unrelated outbox rows
        # from being projected. The workflow diagnostic remains durable.
        return


def _issue_mission_terminal_receipt(
    mission_id: str,
    *,
    state_db_path: Path | None,
    workflow_db_path: Path | None,
) -> tuple[str, str] | None:
    """Best-effort canonical receipt for a mission being terminalized.

    Gated on the profile's configured ``receipts.mode``: ``off`` (the
    shipped default) performs zero receipt/artifact writes and leaves the
    dispatcher byte-identical to its pre-receipt behavior. Under
    ``capture``/``require`` the one canonical receipt is inserted into the
    profile-local state.db BEFORE the caller projects the terminal verdict;
    the caller then links it via ``mdb.project_receipt_verdict`` on its own
    workflows.db connection. Returns ``(receipt_id, status)`` on success and
    ``None`` on any failure — issuance never raises into outbox
    reconciliation, never fabricates messages, and never blocks the
    dispatcher.
    """
    try:
        from agent.receipt_ingest import resolve_configured_receipts_mode

        if resolve_configured_receipts_mode() not in {"capture", "require"}:
            return None

        from agent.receipt_ingest import (
            SnapshotConflictError,
            build_receipt_issuer,
        )
        from agent.receipt_models import ReceiptSourceKey

        wf_path = (
            Path(workflow_db_path)
            if workflow_db_path is not None
            else wfdb.workflows_db_path()
        )
        state_db = SessionDB(db_path=state_db_path)
        try:
            issuer = build_receipt_issuer(
                state_db,
                workflows_db_path=wf_path,
                profile=_active_profile_name(),
            )
            # The dispatcher already holds the workflows.db write
            # transaction and performs the authoritative projection itself
            # (mdb.project_receipt_verdict on its own connection, inside the
            # same transaction). The issuer's built-in cross-connection
            # mission CAS link would contend with that held transaction, so
            # it is disabled here; a crash between receipt insertion and the
            # projection is repaired by ReceiptIssuer.recover_projection.
            issuer._project = lambda source: None  # type: ignore[method-assign]
            source = ReceiptSourceKey("mission", mission_id)
            try:
                receipt = issuer.issue(source)
                return receipt.receipt_id, receipt.status
            except SnapshotConflictError:
                # The mission's durable content changed after issuance: a
                # changed terminal source becomes a recheck observation,
                # never a replacement receipt.
                existing = issuer.store.find_by_source(source)
                if existing is None:
                    raise
                observation = issuer.recheck(existing.receipt_id)
                return existing.receipt_id, observation.status
        finally:
            state_db.close()
    except Exception:
        logger.warning(
            "mission receipt issuance failed for mission=%s; falling back "
            "to the plain verdict projection",
            mission_id,
            exc_info=True,
        )
        return None


def _reconcile_unknown_mission(
    conn: sqlite3.Connection,
    mission: mdb.MissionRecord | None,
    *,
    outbox: SessionDB.OutboxRecord,
    error: dict[str, Any],
    now: int,
    state_db_path: Path | None = None,
    workflow_db_path: Path | None = None,
) -> None:
    if mission is None or mission.profile != _active_profile_name():
        return
    # Terminal mission state is authoritative. In particular, a cancelled or
    # already-verdicted mission must not be rewritten merely because delivery
    # became uncertain later.
    if mission.status not in {"succeeded", "failed", "cancelled"} and mission.verdict is None:
        # The receipt (when receipts.mode enables it) is inserted BEFORE the
        # terminal verdict projection so a crash in between is repaired by
        # source-key recovery instead of losing evidence.
        projection = _issue_mission_terminal_receipt(
            mission.mission_id,
            state_db_path=state_db_path,
            workflow_db_path=workflow_db_path,
        )
        try:
            mdb.set_mission_status(conn, mission.mission_id, "blocked", now=now)
        except mdb.MissionStateError:
            pass
        if projection is not None:
            receipt_id, receipt_status = projection
            try:
                mdb.project_receipt_verdict(
                    conn,
                    mission.mission_id,
                    receipt_id=receipt_id,
                    verdict=receipt_status,
                    now=now,
                )
            except mdb.MissionStateError:
                pass
        else:
            try:
                mdb.set_mission_verdict(conn, mission.mission_id, "unknown_effect", now=now)
            except mdb.MissionStateError:
                pass
    _record_reconciliation_review(
        conn,
        mission,
        outbox=outbox,
        detail={
            "outbox_id": outbox.outbox_id,
            "execution_id": outbox.execution_id,
            "node_id": outbox.node_id,
            "status": outbox.status,
            "result": _projected_outbox_result(outbox),
            "error": error,
        },
        now=now,
        suffix="unknown_effect",
    )


def _resume_terminal_outbox_nodes(
    conn: sqlite3.Connection,
    *,
    now: int,
    state_db_path: Path | None,
    workflow_db_path: Path | None = None,
) -> bool:
    """Project durable outbox terminal states into workflow node runs.

    Delivered rows complete a wait directly. Failed/cancelled rows become a
    workflow failure input so the node's retry/catch policy remains in charge.
    Unknown mission-linked rows are a reconciliation boundary: they block the
    mission and execution and create one deterministic review item.
    """
    waiting_rows = conn.execute(
        """
        SELECT nr.id, nr.execution_id, nr.node_id, nr.outbox_id,
               ex.workflow_id, ex.version, ex.context_json
          FROM workflow_node_runs nr
          JOIN workflow_executions ex ON ex.execution_id = nr.execution_id
         WHERE nr.status = 'waiting'
           AND nr.outbox_id IS NOT NULL
           AND ex.status = 'waiting'
         ORDER BY nr.id
        """
    ).fetchall()
    if not waiting_rows:
        return False

    path_error = _profile_path_error(
        workflow_db_path=workflow_db_path,
        state_db_path=state_db_path,
        node_id=waiting_rows[0]["node_id"],
    )
    if path_error is not None:
        # A terminal row from another profile must never be allowed to mutate
        # this workflow database. The caller stops the tick before any other
        # recovery or execution writes occur.
        return True

    state_db = SessionDB(db_path=state_db_path)
    try:
        store = MissionOutboxStore(state_db)
        terminal_outboxes = {
            row["outbox_id"]: outbox
            for row in waiting_rows
            if (outbox := store.get_by_id(row["outbox_id"])) is not None
            and outbox.status in {"delivered", "failed", "cancelled", "unknown"}
        }
    finally:
        state_db.close()
    if not terminal_outboxes:
        return False

    projection_errors: dict[int, dict[str, Any]] = {}
    expected_missions: dict[int, mdb.MissionRecord | None] = {}
    identity_db = SessionDB(db_path=state_db_path)
    try:
        for row in waiting_rows:
            outbox = terminal_outboxes.get(row["outbox_id"])
            if outbox is None:
                continue
            expected_mission: mdb.MissionRecord | None = None
            try:
                expected_mission = mdb.mission_for_execution(conn, row["execution_id"])
            except (KeyError, mdb.MissionStateError) as exc:
                projection_errors[row["id"]] = _terminal_projection_error(
                    row,
                    outbox,
                    reason=f"expected mission resolution failed: {exc}",
                )
                continue
            expected_missions[row["id"]] = expected_mission
            if (
                outbox.execution_id != row["execution_id"]
                or outbox.node_id != row["node_id"]
            ):
                projection_errors[row["id"]] = _terminal_projection_error(
                    row,
                    outbox,
                    reason=(
                        "outbox execution/node identity does not match waiting node "
                        f"({outbox.execution_id!r}, {outbox.node_id!r})"
                    ),
                    expected_mission_id=(
                        expected_mission.mission_id if expected_mission is not None else None
                    ),
                )
                continue
            if expected_mission is not None and expected_mission.profile != _active_profile_name():
                projection_errors[row["id"]] = _terminal_projection_error(
                    row,
                    outbox,
                    reason=(
                        f"expected mission profile {expected_mission.profile!r} does not "
                        f"match active profile {_active_profile_name()!r}"
                    ),
                    expected_mission_id=expected_mission.mission_id,
                )
                continue
            identity_error = _mission_outbox_identity_error(
                identity_db,
                outbox,
                expected_mission=expected_mission,
            )
            if identity_error is not None:
                projection_errors[row["id"]] = _terminal_projection_error(
                    row,
                    outbox,
                    reason=identity_error,
                    expected_mission_id=(
                        expected_mission.mission_id if expected_mission is not None else None
                    ),
                )
    finally:
        identity_db.close()

    with wfdb.write_txn(conn):
        delivered_executions: set[str] = set()
        for row in waiting_rows:
            outbox = terminal_outboxes.get(row["outbox_id"])
            if outbox is None:
                continue
            status = outbox.status
            projection_error = projection_errors.get(row["id"])
            if projection_error is not None:
                expected_mission = expected_missions.get(row["id"])
                _record_reconciliation_review(
                    conn,
                    expected_mission,
                    outbox=outbox,
                    detail={
                        "outbox_id": outbox.outbox_id,
                        "execution_id": row["execution_id"],
                        "node_id": row["node_id"],
                        "status": status,
                        "result": _projected_outbox_result(outbox),
                        "error": projection_error,
                    },
                    now=now,
                    suffix="identity_mismatch",
                )
                _block_terminal_projection(
                    conn, row, error=projection_error, now=now
                )
                continue
            if status == "delivered":
                output = {
                    "outbox_id": outbox.outbox_id,
                    "outbox_status": status,
                    "result": _projected_outbox_result(outbox),
                    "status": status,
                }
                updated = conn.execute(
                    """
                    UPDATE workflow_node_runs
                       SET status = 'succeeded', output_json = ?, completed_at = ?,
                           wait_until = NULL
                     WHERE id = ? AND status = 'waiting'
                    """,
                    (_json_dumps(output), now, row["id"]),
                ).rowcount
                if updated:
                    delivered_executions.add(row["execution_id"])
                continue

            if status == "unknown" and outbox.mission_id is not None:
                mission = expected_missions.get(row["id"])
                error = {
                    "message": "delivery outcome is unknown; mission blocked for reconciliation",
                    "node": row["node_id"],
                    "phase": "outbox_terminal",
                    "outbox_id": outbox.outbox_id,
                    "outbox_status": status,
                    "mission_id": outbox.mission_id,
                    "reason": "unknown_effect",
                    "reconciliation_required": True,
                }
                _reconcile_unknown_mission(
                    conn,
                    mission,
                    outbox=outbox,
                    error=error,
                    now=now,
                    state_db_path=state_db_path,
                    workflow_db_path=workflow_db_path,
                )
                _block_terminal_projection(conn, row, error=error, now=now)
                continue

            if status == "unknown":
                error = {
                    "message": "delivery outcome is unknown for ordinary workflow; explicit reconciliation required",
                    "node": row["node_id"],
                    "phase": "outbox_terminal",
                    "outbox_id": outbox.outbox_id,
                    "outbox_status": status,
                }
            else:
                error = {
                    "message": f"delivery {status}",
                    "node": row["node_id"],
                    "phase": "outbox_terminal",
                    "outbox_id": outbox.outbox_id,
                    "outbox_status": status,
                    "result": _projected_outbox_result(outbox),
                }
            spec = wfdb.get_definition(conn, row["workflow_id"], row["version"])
            node = spec.nodes.get(row["node_id"])
            if node is not None and (
                node.catch or (status != "unknown" and node.retry is not None)
            ):
                context = json.loads(row["context_json"])
                context["_terminal_outbox_error"] = error
                conn.execute(
                    """
                    UPDATE workflow_node_runs
                       SET status = 'queued', outbox_id = NULL, wait_until = ?
                     WHERE id = ? AND status = 'waiting'
                    """,
                    (now, row["id"]),
                )
                conn.execute(
                    """
                    UPDATE workflow_executions
                       SET status = 'queued', context_json = ?,
                           claim_lock = NULL, claim_expires = NULL, updated_at = ?
                     WHERE execution_id = ? AND status = 'waiting'
                    """,
                    (_json_dumps(context), now, row["execution_id"]),
                )
            else:
                updated = conn.execute(
                    """
                    UPDATE workflow_node_runs
                       SET status = 'failed', error = ?, completed_at = ?, wait_until = NULL
                     WHERE id = ? AND status = 'waiting'
                    """,
                    (_json_dumps(error), now, row["id"]),
                ).rowcount
                if updated:
                    conn.execute(
                        """
                        UPDATE workflow_executions
                           SET status = 'failed', context_json = ?,
                               claim_lock = NULL, claim_expires = NULL, updated_at = ?
                         WHERE execution_id = ? AND status IN ('waiting', 'queued')
                        """,
                        (_json_dumps(_context_with_error(json.loads(row["context_json"]), error)), now, row["execution_id"]),
                    )
                    _append_event(conn, row["execution_id"], "execution_failed", {"error": error}, now)
        for execution_id in delivered_executions:
            conn.execute(
                """
                UPDATE workflow_executions
                   SET status = 'queued', claim_lock = NULL,
                       claim_expires = NULL, updated_at = ?
                 WHERE execution_id = ? AND status = 'waiting'
                """,
                (now, execution_id),
            )
    return False


def _resume_due_waits(conn: sqlite3.Connection, *, now: int) -> None:
    with wfdb.write_txn(conn):
        rows = conn.execute(
            """
            SELECT nr.id, nr.execution_id
              FROM workflow_node_runs nr
              JOIN workflow_executions ex ON ex.execution_id = nr.execution_id
             WHERE nr.status = 'waiting'
               AND nr.wait_until IS NOT NULL
               AND nr.wait_until <= ?
               AND ex.status = 'waiting'
             ORDER BY nr.wait_until, nr.id
            """,
            (now,),
        ).fetchall()
        for row in rows:
            updated = conn.execute(
                """
                UPDATE workflow_node_runs
                   SET status = 'succeeded', completed_at = ?
                 WHERE id = ? AND status = 'waiting'
                """,
                (now, row["id"]),
            ).rowcount
            if updated:
                conn.execute(
                    """
                    UPDATE workflow_executions
                       SET status = 'queued', claim_lock = NULL,
                           claim_expires = NULL, updated_at = ?
                     WHERE execution_id = ? AND status = 'waiting'
                    """,
                    (now, row["execution_id"]),
                )


def _resume_due_retries(conn: sqlite3.Connection, *, now: int) -> None:
    with wfdb.write_txn(conn):
        rows = conn.execute(
            """
            SELECT DISTINCT nr.execution_id
              FROM workflow_node_runs nr
              JOIN workflow_executions ex ON ex.execution_id = nr.execution_id
             WHERE nr.status = 'queued'
               AND (nr.wait_until IS NULL OR nr.wait_until <= ?)
               AND ex.status = 'waiting'
             ORDER BY nr.wait_until, nr.id
            """,
            (now,),
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                UPDATE workflow_executions
                   SET status = 'queued', claim_lock = NULL,
                       claim_expires = NULL, updated_at = ?
                 WHERE execution_id = ? AND status = 'waiting'
                """,
                (now, row["execution_id"]),
            )


def _start_ready_feed_items(conn: sqlite3.Connection, *, now: int, limit: int) -> int:
    started = 0
    while started < limit:
        with wfdb.write_txn(conn):
            item = wfdb.claim_next_ready_input_item(conn)
            if item is None:
                break
            try:
                execution_id = wfdb.start_execution(
                    conn,
                    item.workflow_id,
                    input_data=item.input,
                    trigger_type="input_feed",
                    trigger_id=item.trigger_id,
                    version=item.version,
                    now=now,
                )
            except (KeyError, ValueError):
                wfdb.mark_input_item_terminal(conn, item.item_id, "failed", now=now)
                started += 1
                continue
            wfdb.mark_input_item_running(conn, item.item_id, execution_id, now=now)
        started += 1
    return started


def _render_agent_prompt(node: Any, context: dict[str, Any]) -> str:
    return render_agent_prompt(node.prompt, context)


def _render_agent_task_title(
    node: Any,
    *,
    spec: WorkflowSpec,
    node_id: str,
    context: dict[str, Any],
) -> str:
    raw = (node.title or f"{spec.name}: {node_id}").strip()
    if not isinstance(node.title, str) or not node.title.strip():
        return raw
    try:
        rendered = render_prompt_text(node.title, context).strip()
    except (KeyError, ValueError):
        return raw
    return rendered or raw


def _create_or_get_agent_task(
    *,
    execution_id: str,
    spec: WorkflowSpec,
    node_id: str,
    node: Any,
    context: dict[str, Any],
) -> tuple[str, str]:
    board = kb.get_current_board()
    with kb.connect_closing(board=board) as kconn:
        task_id = kb.create_task(
            kconn,
            title=_render_agent_task_title(node, spec=spec, node_id=node_id, context=context),
            body=_render_agent_prompt(node, context),
            assignee=node.profile,
            created_by=f"workflow:{execution_id}:version:{spec.version}:node:{node_id}",
            workspace_kind=node.workspace_kind or "scratch",
            workspace_path=node.workspace_path,
            skills=node.skills or None,
            max_retries=node.max_retries,
            goal_mode=bool(node.goal_mode),
            goal_max_turns=node.goal_max_turns,
            workflow_template_id=spec.id,
            current_step_key=node_id,
            provider_override=node.provider_override,
            model_override=node.model_override,
            idempotency_key=f"workflow:{execution_id}:{node_id}",
        )
        return task_id, board


def _parse_agent_result(raw: str | None) -> Any:
    if raw is None or raw == "":
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {"result": raw}


def _validate_result_contract(output: Any, contract: dict[str, Any]) -> list[str]:
    errors = []
    if not contract:
        return errors
    if not isinstance(output, dict):
        return ["agent result must be a JSON object to satisfy result_contract"]
    for key, expected in contract.items():
        if key not in output:
            errors.append(f"missing required result key: {key}")
            continue
        value = output[key]
        if not isinstance(expected, str):
            errors.append(f"result key {key} contract must be string")
            continue
        expected = expected.strip()
        if expected == "string" and not isinstance(value, str):
            errors.append(f"result key {key} must be string")
        elif expected == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            errors.append(f"result key {key} must be number")
        elif expected == "boolean" and not isinstance(value, bool):
            errors.append(f"result key {key} must be boolean")
        elif expected == "array" and not isinstance(value, list):
            errors.append(f"result key {key} must be array")
        elif expected == "object" and not isinstance(value, dict):
            errors.append(f"result key {key} must be object")
        elif expected in RESULT_CONTRACT_PRIMITIVES:
            continue
        elif "|" in expected:
            allowed = {part.strip() for part in expected.split("|") if part.strip()}
            if not allowed:
                errors.append(f"result key {key} has empty result_contract enum")
                continue
            actual = "true" if value is True else "false" if value is False else str(value)
            if actual not in allowed:
                errors.append(f"result key {key} must be one of {sorted(allowed)}")
        else:
            errors.append(f"result key {key} has invalid result_contract token: {expected}")
    return errors


def _kanban_block_reason(conn: sqlite3.Connection, task_id: str) -> str:
    row = conn.execute(
        """
        SELECT payload FROM task_events
         WHERE task_id = ? AND kind IN ('blocked', 'dependency_wait', 'block_loop_detected', 'gave_up')
         ORDER BY id DESC LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if row is not None:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        for key in ("reason", "error", "message"):
            reason = payload.get(key)
            if isinstance(reason, str) and reason:
                return reason
    row = conn.execute(
        "SELECT last_failure_error FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is not None and row["last_failure_error"]:
        return row["last_failure_error"]
    row = conn.execute(
        """
        SELECT error, summary FROM task_runs
         WHERE task_id = ?
           AND ((error IS NOT NULL AND error != '') OR (summary IS NOT NULL AND summary != ''))
         ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if row is not None:
        return row["error"] or row["summary"]
    return "kanban task blocked"


def _block_agent_node(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    error: dict[str, Any],
    now: int,
) -> None:
    try:
        context = json.loads(row["context_json"] or "{}")
    except (TypeError, ValueError):
        context = {"node": {}}
    context["error"] = error
    sibling_refs: list[dict[str, Any]] = []
    with wfdb.write_txn(conn):
        updated = conn.execute(
            """
            UPDATE workflow_node_runs
               SET status = 'blocked', error = ?, completed_at = ?
             WHERE id = ? AND status = 'waiting'
            """,
            (_json_dumps(error), now, row["id"]),
        ).rowcount
        if updated:
            conn.execute(
                """
                UPDATE workflow_executions
                   SET status = 'blocked', context_json = ?, claim_lock = NULL,
                       claim_expires = NULL, updated_at = ?
                 WHERE execution_id = ? AND status = 'waiting'
                """,
                (_json_dumps(context), now, row["execution_id"]),
            )
            sibling_refs = _linked_waiting_kanban_task_refs(conn, row["execution_id"])
            if sibling_refs:
                sibling_ids = [ref["node_run_id"] for ref in sibling_refs]
                placeholders = ", ".join("?" for _ in sibling_ids)
                conn.execute(
                    f"""
                    UPDATE workflow_node_runs
                       SET status = 'blocked', error = ?, completed_at = ?, wait_until = NULL
                     WHERE id IN ({placeholders})
                    """,
                    (_json_dumps(error), now, *sibling_ids),
                )
            _append_event(conn, row["execution_id"], "execution_blocked", error, now)
    if sibling_refs:
        wfdb.block_linked_kanban_tasks(
            [(ref["task_id"], ref["kanban_board"]) for ref in sibling_refs],
            execution_id=row["execution_id"],
            source="agent_task_block",
            reason=f"workflow execution {row['execution_id']} blocked by node {row['node_id']}",
        )


def _resume_completed_agent_tasks(conn: sqlite3.Connection, *, now: int) -> None:
    rows = conn.execute(
        """
        SELECT nr.id, nr.execution_id, nr.node_id, nr.kanban_task_id, nr.kanban_board,
               ex.context_json, ex.workflow_id, ex.version
          FROM workflow_node_runs nr
          JOIN workflow_executions ex ON ex.execution_id = nr.execution_id
         WHERE nr.status = 'waiting'
           AND nr.kanban_task_id IS NOT NULL
           AND ex.status = 'waiting'
         ORDER BY nr.id
        """
    ).fetchall()
    if not rows:
        return

    for row in rows:
        with kb.connect_closing(board=row["kanban_board"]) as kconn:
            task = kb.get_task(kconn, row["kanban_task_id"])
            if task is None:
                continue
            if task.status == "done":
                output = _parse_agent_result(task.result or kb.latest_summary(kconn, task.id))
                spec = wfdb.get_definition(conn, row["workflow_id"], row["version"])
                node = spec.nodes.get(row["node_id"])
                contract = node.result_contract if node is not None else {}
                errors = _validate_result_contract(output, contract)
                if errors:
                    _block_agent_node(
                        conn,
                        row,
                        error={
                            "node_id": row["node_id"],
                            "kanban_task_id": task.id,
                            "reason": "; ".join(errors),
                        },
                        now=now,
                    )
                    continue
                with wfdb.write_txn(conn):
                    updated = conn.execute(
                        """
                        UPDATE workflow_node_runs
                           SET status = 'succeeded', output_json = ?, completed_at = ?
                         WHERE id = ? AND status = 'waiting'
                        """,
                        (_json_dumps(output), now, row["id"]),
                    ).rowcount
                    if updated:
                        conn.execute(
                            """
                            UPDATE workflow_executions
                               SET status = 'queued', claim_lock = NULL,
                                   claim_expires = NULL, updated_at = ?
                             WHERE execution_id = ? AND status = 'waiting'
                            """,
                            (now, row["execution_id"]),
                        )
            elif task.status == "blocked":
                _block_agent_node(
                    conn,
                    row,
                    error={
                        "node_id": row["node_id"],
                        "kanban_task_id": task.id,
                        "reason": _kanban_block_reason(kconn, task.id),
                    },
                    now=now,
                )


def _completed_wait_nodes(conn: sqlite3.Connection, execution_id: str) -> set[str]:
    return {
        row["node_id"]
        for row in conn.execute(
            """
            SELECT node_id FROM workflow_node_runs
             WHERE execution_id = ?
               AND status = 'succeeded'
               AND (wait_until IS NOT NULL OR outbox_id IS NOT NULL)
            """,
            (execution_id,),
        )
    }


def _completed_node_outputs(conn: sqlite3.Connection, execution_id: str) -> dict[str, Any]:
    outputs: dict[str, Any] = {}
    rows = conn.execute(
        """
        SELECT node_id, output_json FROM workflow_node_runs
         WHERE execution_id = ?
           AND status = 'succeeded'
           AND output_json IS NOT NULL
        """,
        (execution_id,),
    ).fetchall()
    for row in rows:
        try:
            outputs[row["node_id"]] = json.loads(row["output_json"])
        except (TypeError, ValueError):
            continue
    return outputs


def _materialize_send_message(
    conn: sqlite3.Connection,
    *,
    execution_id: str,
    node_id: str,
    node: Any,
    context: dict[str, Any],
    now: int,
    state_db_path: Path | None,
    workflow_db_path: Path | None = None,
    requeue_cancelled: bool = False,
    requeue_terminal: bool = False,
) -> str:
    try:
        platform = render_template(node.platform, context)
        target = render_template(node.target, context)
        content = render_template(node.message, context)
    except Exception as exc:
        raise _SendMessageMaterializationError(node_id, f"template render failed: {exc}") from exc

    platform = normalize_platform_token(platform)
    if platform is None:
        raise _SendMessageMaterializationError(
            node_id, "rendered platform must be a safe non-blank token"
        )
    if not isinstance(target, str) or not target.strip():
        raise _SendMessageMaterializationError(node_id, "rendered target must be a non-blank string")
    target = target.strip()
    if isinstance(content, str):
        if not content.strip():
            raise _SendMessageMaterializationError(node_id, "rendered message must be non-blank")
    elif not isinstance(content, dict) or not content:
        raise _SendMessageMaterializationError(
            node_id, "rendered message must be a non-empty string or mapping"
        )

    try:
        max_delay_seconds = _max_outbox_delay_seconds()
    except ValueError as exc:
        raise _SendMessageMaterializationError(node_id, str(exc)) from exc
    if node.not_before_seconds > max_delay_seconds:
        raise _SendMessageMaterializationError(
            node_id,
            "not_before_seconds exceeds configured maximum "
            f"of {max_delay_seconds} seconds",
        )

    try:
        mission = mdb.mission_for_execution(conn, execution_id)
    except mdb.MissionStateError as exc:
        raise _SendMessageMaterializationError(node_id, f"mission lookup failed: {exc}") from exc
    if mission is not None:
        path_error = _profile_path_error(
            workflow_db_path=workflow_db_path,
            state_db_path=state_db_path,
            node_id=node_id,
        )
        if path_error is not None:
            raise _SendMessageMaterializationError(node_id, path_error)
        if mission.profile != _active_profile_name():
            raise _SendMessageMaterializationError(
                node_id,
                f"mission profile {mission.profile!r} does not match active profile "
                f"{_active_profile_name()!r}",
            )
    if mission is not None and mission.status != "running":
        raise _SendMessageMaterializationError(
            node_id, f"mission status {mission.status!r} does not permit materialization"
        )
    if mission is not None and mission.verdict is not None:
        raise _SendMessageMaterializationError(
            node_id, "mission verdict does not permit materialization"
        )
    if mission is not None:
        authority = mission.authority
        if authority.get("revoked", False) is not False or authority.get("valid", True) is not True:
            raise _SendMessageMaterializationError(
                node_id, "mission authority is revoked or invalid"
            )
        expires_at = authority.get("expires_at")
        if not isinstance(expires_at, int) or isinstance(expires_at, bool):
            raise _SendMessageMaterializationError(
                node_id, "mission authority requires an integer expires_at"
            )
        if now >= expires_at:
            raise _SendMessageMaterializationError(node_id, "mission authority is expired")
        allowed_effects = authority.get("allowed_effects")
        if not isinstance(allowed_effects, list) or "delayed_message" not in allowed_effects:
            raise _SendMessageMaterializationError(
                node_id, "mission authority does not allow delayed_message"
            )
        if not _authority_allows_destination(
            authority, platform=platform, target=target
        ):
            raise _SendMessageMaterializationError(
                node_id, f"message target {target!r} is not authorized for this mission"
            )

    state_db = SessionDB(db_path=state_db_path)
    try:
        store = MissionOutboxStore(state_db)
        materialize = (
            store.requeue_terminal
            if requeue_terminal or requeue_cancelled
            else store.materialize
        )
        outbox = materialize(
            execution_id=execution_id,
            node_id=node_id,
            mission_id=mission.mission_id if mission is not None else None,
            platform=platform,
            target=target,
            content=content,
            not_before=now + node.not_before_seconds,
        )
    except Exception as exc:
        raise _SendMessageMaterializationError(node_id, str(exc)) from exc
    finally:
        state_db.close()
    return outbox.outbox_id


def _cancel_unclaimed_outboxes(
    state_db_path: Path | None,
    outbox_ids: set[str],
) -> None:
    """Compensate outboxes materialized before workflow persistence completed.

    Unclaimed rows are cancelled so stale payloads cannot be delivered. A row
    that has already been claimed is uncertain and is quarantined as unknown;
    cancelling it would hide a possible external delivery.
    """
    if not outbox_ids:
        return
    state_db = SessionDB(db_path=state_db_path)
    try:
        store = MissionOutboxStore(state_db)
        for outbox_id in outbox_ids:
            outcome = store.compensate(outbox_id)
            if outcome not in {"cancelled", "unknown", "terminal"}:
                raise RuntimeError(
                    f"outbox compensation returned unexpected outcome {outcome!r}"
                )
    finally:
        state_db.close()


def _persist_waiting_nodes(
    conn: sqlite3.Connection,
    *,
    execution_id: str,
    result: EngineResult,
    spec: WorkflowSpec | None,
    now: int,
    state_db_path: Path | None,
    workflow_db_path: Path | None = None,
    materialized_outbox_ids: set[str] | None = None,
) -> None:
    if result.status != "waiting":
        return
    attempted_send_nodes: set[str] = set()
    attempted_outbox_ids: set[str] = set()
    current_node_id: str | None = None
    try:
        for node_id in result.waiting_nodes:
            current_node_id = node_id
            node = spec.nodes.get(node_id) if spec is not None else None
            if node is not None and node.type == "wait":
                wait_until = now + node.seconds
            else:
                wait_until = None
            kanban_task_id = None
            kanban_board = None
            exists = conn.execute(
                """
                SELECT id, kanban_task_id, kanban_board, outbox_id FROM workflow_node_runs
                 WHERE execution_id = ? AND node_id = ? AND status = 'waiting'
                """,
                (execution_id, node_id),
            ).fetchone()
            outbox_id = exists["outbox_id"] if exists is not None else None
            if outbox_id:
                attempted_outbox_ids.add(outbox_id)
                if materialized_outbox_ids is not None:
                    materialized_outbox_ids.add(outbox_id)
            if spec is not None and node is not None and node.type == "agent_task":
                try:
                    kanban_task_id, kanban_board = _create_or_get_agent_task(
                        execution_id=execution_id,
                        spec=spec,
                        node_id=node_id,
                        node=node,
                        context=result.context,
                    )
                except Exception as exc:
                    raise _AgentTaskMaterializationError(node_id, str(exc)) from exc
            elif node is not None and node.type == "send_message" and not outbox_id:
                attempted_send_nodes.add(node_id)
                state_db = SessionDB(db_path=state_db_path)
                try:
                    preexisting = state_db.get_outbox_by_identity(execution_id, node_id)
                finally:
                    state_db.close()
                requeue_terminal = preexisting is not None and preexisting.status in {
                    "cancelled",
                    "failed",
                }
                outbox_id = _materialize_send_message(
                    conn,
                    execution_id=execution_id,
                    node_id=node_id,
                    node=node,
                    context=result.context,
                    now=now,
                    state_db_path=state_db_path,
                    workflow_db_path=workflow_db_path,
                    requeue_terminal=requeue_terminal,
                )
                attempted_outbox_ids.add(outbox_id)
                if materialized_outbox_ids is not None:
                    materialized_outbox_ids.add(outbox_id)
            if exists is None:
                conn.execute(
                    """
                    INSERT INTO workflow_node_runs (
                        execution_id, node_id, status, started_at, wait_until,
                        kanban_task_id, kanban_board, outbox_id
                    ) VALUES (?, ?, 'waiting', ?, ?, ?, ?, ?)
                    """,
                    (execution_id, node_id, now, wait_until, kanban_task_id, kanban_board, outbox_id),
                )
            elif kanban_task_id and (not exists["kanban_task_id"] or not exists["kanban_board"]):
                conn.execute(
                    "UPDATE workflow_node_runs SET kanban_task_id = ?, kanban_board = ? WHERE id = ?",
                    (kanban_task_id, kanban_board, exists["id"]),
                )
            elif outbox_id and not exists["outbox_id"]:
                conn.execute(
                    "UPDATE workflow_node_runs SET outbox_id = ? WHERE id = ?",
                    (outbox_id, exists["id"]),
                )
    except Exception as exc:
        # The outbox lives in state.db, outside the workflow transaction. If
        # node-run persistence fails after materialization, compensate every
        # durable identity attached during this attempt, including a reused
        # scheduled orphan.
        for node_id in attempted_send_nodes:
            state_db = SessionDB(db_path=state_db_path)
            try:
                current = state_db.get_outbox_by_identity(execution_id, node_id)
                if current is not None:
                    attempted_outbox_ids.add(current.outbox_id)
            finally:
                state_db.close()
        _cancel_unclaimed_outboxes(state_db_path, attempted_outbox_ids)
        if isinstance(exc, (_AgentTaskMaterializationError, _SendMessageMaterializationError)):
            raise
        if current_node_id in attempted_send_nodes:
            raise _SendMessageMaterializationError(
                current_node_id,
                f"waiting node persistence failed: {exc}",
            ) from exc
        raise


def _failed_node_id(result: EngineResult, spec: WorkflowSpec | None) -> str | None:
    if result.status != "failed" or spec is None or not result.error:
        return None
    node_id = result.error.get("node")
    if isinstance(node_id, str) and node_id in spec.nodes:
        return node_id
    return None


def _context_with_error(context: dict[str, Any], error: dict[str, Any]) -> dict[str, Any]:
    updated = dict(context)
    updated.setdefault("node", {})
    updated["error"] = error
    return updated


def _linked_waiting_kanban_task_refs(conn: sqlite3.Connection, execution_id: str) -> list[dict[str, Any]]:
    return [
        {
            "node_run_id": row["id"],
            "task_id": row["kanban_task_id"],
            "kanban_board": row["kanban_board"],
        }
        for row in conn.execute(
            """
            SELECT id, kanban_task_id, kanban_board
              FROM workflow_node_runs
             WHERE execution_id = ?
               AND status = 'waiting'
               AND kanban_task_id IS NOT NULL
            """,
            (execution_id,),
        ).fetchall()
    ]


def _materialization_failure_result(
    conn: sqlite3.Connection,
    *,
    execution_id: str,
    result: EngineResult,
    exc: _AgentTaskMaterializationError | _SendMessageMaterializationError,
    now: int,
) -> EngineResult:
    error = {
        "message": str(exc),
        "node": exc.node_id,
        "phase": exc.phase,
    }
    linked_task_refs = _linked_waiting_kanban_task_refs(conn, execution_id)
    wfdb.block_linked_kanban_tasks(
        [(ref["task_id"], ref["kanban_board"]) for ref in linked_task_refs],
        execution_id=execution_id,
        source="agent_task_materialization",
        reason=f"workflow execution {execution_id} failed to create agent task {exc.node_id}: {exc}",
    )
    if linked_task_refs:
        linked_node_run_ids = [ref["node_run_id"] for ref in linked_task_refs]
        placeholders = ", ".join("?" for _ in linked_node_run_ids)
        conn.execute(
            f"""
            UPDATE workflow_node_runs
               SET status = 'blocked', error = ?, completed_at = ?, wait_until = NULL
             WHERE id IN ({placeholders})
            """,
            (_json_dumps(error), now, *linked_node_run_ids),
        )
    return EngineResult(
        status="failed",
        context=_context_with_error(result.context, error),
        waiting_nodes=[],
        error=error,
    )


def _persist_failed_attempt(
    conn: sqlite3.Connection,
    *,
    execution_id: str,
    node_id: str,
    error: dict[str, Any],
    now: int,
) -> None:
    queued = conn.execute(
        """
        SELECT id FROM workflow_node_runs
         WHERE execution_id = ? AND node_id = ? AND status = 'queued'
         ORDER BY id DESC LIMIT 1
        """,
        (execution_id, node_id),
    ).fetchone()
    if queued is not None:
        conn.execute(
            """
            UPDATE workflow_node_runs
               SET status = 'failed', error = ?, started_at = COALESCE(started_at, ?),
                   completed_at = ?, wait_until = NULL
             WHERE id = ?
            """,
            (_json_dumps(error), now, now, queued["id"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO workflow_node_runs (
                execution_id, node_id, status, error, started_at, completed_at
            ) VALUES (?, ?, 'failed', ?, ?, ?)
            """,
            (execution_id, node_id, _json_dumps(error), now, now),
        )
    _append_event(
        conn,
        execution_id,
        "node_failed",
        {"node_id": node_id, "error": error},
        now,
    )


def _persist_successful_queued_attempts(
    conn: sqlite3.Connection,
    *,
    execution_id: str,
    result: EngineResult,
    now: int,
) -> None:
    for node_id, node_context in result.context.get("node", {}).items():
        output_json = None
        if isinstance(node_context, dict) and "output" in node_context:
            output_json = _json_dumps(node_context["output"])
        conn.execute(
            """
            UPDATE workflow_node_runs
               SET status = 'succeeded', output_json = ?, completed_at = ?, wait_until = NULL
             WHERE id = (
                SELECT id FROM workflow_node_runs
                 WHERE execution_id = ? AND node_id = ? AND status = 'queued'
                 ORDER BY id DESC LIMIT 1
             )
            """,
            (output_json, now, execution_id, node_id),
        )


def _failed_attempts(conn: sqlite3.Connection, execution_id: str, node_id: str) -> int:
    return int(conn.execute(
        """
        SELECT count(*) FROM workflow_node_runs
         WHERE execution_id = ? AND node_id = ? AND status = 'failed'
        """,
        (execution_id, node_id),
    ).fetchone()[0])


def _catch_resume_kwargs(
    conn: sqlite3.Connection,
    *,
    execution_id: str,
    context: dict[str, Any],
    spec: WorkflowSpec,
) -> dict[str, Any]:
    error = context.get("error")
    if not isinstance(error, dict):
        return {}
    node_id = error.get("node")
    if not isinstance(node_id, str) or node_id not in spec.nodes or not spec.nodes[node_id].catch:
        return {}
    queued_retry = conn.execute(
        """
        SELECT 1 FROM workflow_node_runs
         WHERE execution_id = ? AND node_id = ? AND status = 'queued'
         LIMIT 1
        """,
        (execution_id, node_id),
    ).fetchone()
    if queued_retry is not None:
        return {}
    return {"catch_failed_nodes": {node_id}, "error_context": error}


def _retry_due_at(node: Any, *, failed_attempts: int, now: int) -> int:
    retry = node.retry
    base = retry.backoff_seconds if retry.backoff_seconds is not None else retry.delay_seconds
    return int(now + base * (retry.multiplier ** max(0, failed_attempts - 1)))


def _emit_progress_events(
    conn: sqlite3.Connection,
    *,
    execution_id: str,
    result: EngineResult,
    spec: WorkflowSpec | None,
    now: int,
    state_db_path: Path | None,
    workflow_db_path: Path | None,
    existing_events: list[sqlite3.Row],
    materialized_outbox_ids: set[str] | None = None,
) -> None:
    emitted_nodes: set[str] = set()
    for event in existing_events:
        if event["kind"] != "node_succeeded":
            continue
        try:
            payload = json.loads(event["payload_json"])
        except (TypeError, ValueError):
            continue
        node_id = payload.get("node_id")
        if isinstance(node_id, str):
            emitted_nodes.add(node_id)

    _persist_waiting_nodes(
        conn,
        execution_id=execution_id,
        result=result,
        spec=spec,
        now=now,
        state_db_path=state_db_path,
        workflow_db_path=workflow_db_path,
        materialized_outbox_ids=materialized_outbox_ids,
    )
    if not existing_events:
        _append_event(conn, execution_id, "execution_started", {}, now)
    for node_id, node_context in result.context.get("node", {}).items():
        if node_id in emitted_nodes:
            continue
        output = node_context.get("output") if isinstance(node_context, dict) else None
        _append_event(
            conn,
            execution_id,
            "node_succeeded",
            {"node_id": node_id, "output": output},
            now,
        )
        emitted_nodes.add(node_id)


def _fail_if_waiting_unresumable(
    conn: sqlite3.Connection,
    *,
    execution_id: str,
    result: EngineResult,
) -> EngineResult:
    if result.status != "waiting":
        return result
    rows = conn.execute(
        """
        SELECT node_id, wait_until, kanban_task_id, outbox_id
          FROM workflow_node_runs
         WHERE execution_id = ? AND status = 'waiting'
        """,
        (execution_id,),
    ).fetchall()
    stuck_nodes = [row["node_id"] for row in rows]
    if not stuck_nodes:
        stuck_nodes = list(result.waiting_nodes)
    if not stuck_nodes:
        return result
    resumable = any(
        row["wait_until"] is not None
        or row["kanban_task_id"] is not None
        or row["outbox_id"] is not None
        for row in rows
    )
    if resumable:
        return result
    return EngineResult(
        status="failed",
        context=result.context,
        waiting_nodes=[],
        error={
            "message": "workflow waiting on unresumable node(s): " + ", ".join(stuck_nodes),
            "waiting_nodes": stuck_nodes,
        },
    )


def _catch_spec_for_node(
    spec: WorkflowSpec,
    *,
    node_id: str,
    error: dict[str, Any],
) -> WorkflowSpec:
    node = spec.nodes[node_id]
    if node.type == "fail":
        return spec
    # The engine's native catch hook is attached to fail nodes. A terminal
    # delivery error is already a node failure, so use a validated fail-node
    # projection solely for catch routing without re-dispatching the message.
    failed_node = node.model_copy(update={"type": "fail", "output": error})
    return spec.model_copy(update={"nodes": {**spec.nodes, node_id: failed_node}})


def _apply_failure_semantics(
    conn: sqlite3.Connection,
    *,
    execution_id: str,
    result: EngineResult,
    spec: WorkflowSpec | None,
    now: int,
    token: str,
    input_json: str,
    existing_events: list[sqlite3.Row],
    state_db_path: Path | None,
    workflow_db_path: Path | None,
    materialized_outbox_ids: set[str] | None = None,
) -> tuple[EngineResult, bool]:
    node_id = _failed_node_id(result, spec)
    if node_id is None or spec is None:
        return result, False
    error = result.error or {}
    result.context = _context_with_error(result.context, error)
    result.context.pop("_terminal_outbox_error", None)
    _persist_failed_attempt(
        conn,
        execution_id=execution_id,
        node_id=node_id,
        error=error,
        now=now,
    )
    failed_attempts = _failed_attempts(conn, execution_id, node_id)
    node = spec.nodes[node_id]
    terminal_unknown = (
        error.get("phase") == "outbox_terminal"
        and error.get("outbox_status") == "unknown"
    )
    if node.retry is not None and not terminal_unknown and failed_attempts < node.retry.max_attempts:
        due_at = _retry_due_at(node, failed_attempts=failed_attempts, now=now)
        status = "waiting" if due_at > now else "queued"
        conn.execute(
            """
            INSERT INTO workflow_node_runs (
                execution_id, node_id, status, started_at, wait_until
            ) VALUES (?, ?, 'queued', ?, ?)
            """,
            (execution_id, node_id, now, due_at),
        )
        _emit_progress_events(
            conn,
            execution_id=execution_id,
            result=result,
            spec=spec,
            now=now,
            state_db_path=state_db_path,
            workflow_db_path=workflow_db_path,
            existing_events=existing_events,
            materialized_outbox_ids=materialized_outbox_ids,
        )
        if status == "waiting":
            _append_event(conn, execution_id, "execution_waiting", {"waiting_nodes": []}, now)
        conn.execute(
            """
            UPDATE workflow_executions
               SET status = ?, context_json = ?, claim_lock = NULL,
                   claim_expires = NULL, updated_at = ?
             WHERE execution_id = ? AND claim_lock = ?
            """,
            (status, _json_dumps(result.context), now, execution_id, token),
        )
        return result, True
    if node.catch:
        completed_wait_nodes = _completed_wait_nodes(conn, execution_id)
        completed_outputs = _completed_node_outputs(conn, execution_id)
        kwargs: dict[str, Any] = {
            "catch_failed_nodes": {node_id},
            "error_context": error,
        }
        if completed_wait_nodes:
            kwargs["completed_wait_nodes"] = completed_wait_nodes
        if completed_outputs:
            kwargs["completed_node_outputs"] = completed_outputs
        catch_context = _context_with_error(result.context, error)
        try:
            result = run_in_memory_until_waiting(
                _catch_spec_for_node(spec, node_id=node_id, error=error),
                json.loads(input_json),
                **kwargs,
            )
        except Exception as exc:
            result = EngineResult(
                status="failed",
                context=catch_context,
                waiting_nodes=[],
                error={
                    "message": str(exc),
                    "catch_node": node.catch,
                    "caught_node": node_id,
                },
            )
        else:
            result.context = _context_with_error(result.context, error)
    return result, False


def _finish_transaction(
    conn: sqlite3.Connection,
    *,
    execution_id: str,
    token: str,
    result: EngineResult,
    spec: WorkflowSpec | None,
    now: int,
    state_db_path: Path | None = None,
    workflow_db_path: Path | None = None,
    materialized_outbox_ids: set[str] | None = None,
) -> bool:
    with wfdb.write_txn(conn):
        row = conn.execute(
            "SELECT claim_lock, input_json FROM workflow_executions WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        if row is None or row["claim_lock"] != token:
            return False

        existing_events = conn.execute(
            "SELECT kind, payload_json FROM workflow_events WHERE execution_id = ?",
            (execution_id,),
        ).fetchall()

        result, handled_failure = _apply_failure_semantics(
            conn,
            execution_id=execution_id,
            result=result,
            spec=spec,
            now=now,
            token=token,
            input_json=row["input_json"],
            existing_events=existing_events,
            state_db_path=state_db_path,
            workflow_db_path=workflow_db_path,
            materialized_outbox_ids=materialized_outbox_ids,
        )
        if handled_failure:
            return True

        _persist_successful_queued_attempts(
            conn,
            execution_id=execution_id,
            result=result,
            now=now,
        )
        try:
            _emit_progress_events(
                conn,
                execution_id=execution_id,
                result=result,
                spec=spec,
                now=now,
                state_db_path=state_db_path,
                workflow_db_path=workflow_db_path,
                existing_events=existing_events,
                materialized_outbox_ids=materialized_outbox_ids,
            )
            result = _fail_if_waiting_unresumable(
                conn,
                execution_id=execution_id,
                result=result,
            )
            if result.status == "failed" and result.error and result.error.get("waiting_nodes"):
                conn.execute(
                    """
                    UPDATE workflow_node_runs
                       SET status = 'failed', error = ?, completed_at = ?, wait_until = NULL
                     WHERE execution_id = ?
                       AND status = 'waiting'
                       AND wait_until IS NULL
                       AND kanban_task_id IS NULL
                    """,
                    (_json_dumps(result.error), now, execution_id),
                )
        except (_AgentTaskMaterializationError, _SendMessageMaterializationError) as exc:
            result = _materialization_failure_result(
                conn,
                execution_id=execution_id,
                result=result,
                exc=exc,
                now=now,
            )
            result, handled_failure = _apply_failure_semantics(
                conn,
                execution_id=execution_id,
                result=result,
                spec=spec,
                now=now,
                token=token,
                input_json=row["input_json"],
                existing_events=existing_events,
                state_db_path=state_db_path,
                workflow_db_path=workflow_db_path,
                materialized_outbox_ids=materialized_outbox_ids,
            )
            if handled_failure:
                return True
        if result.status == "succeeded":
            final_event = "execution_succeeded"
            final_payload = {}
        elif result.status == "waiting":
            final_event = "execution_waiting"
            final_payload = {"waiting_nodes": result.waiting_nodes}
        else:
            final_event = "execution_failed"
            final_payload = {"error": result.error or {}}
        _append_event(conn, execution_id, final_event, final_payload, now)
        conn.execute(
            """
            UPDATE workflow_executions
               SET status = ?, context_json = ?, claim_lock = NULL,
                   claim_expires = NULL, updated_at = ?
             WHERE execution_id = ? AND claim_lock = ?
            """,
            (result.status, _json_dumps(result.context), now, execution_id, token),
        )
    return True


def _finish(
    conn: sqlite3.Connection,
    *,
    execution_id: str,
    token: str,
    result: EngineResult,
    spec: WorkflowSpec | None,
    now: int,
    state_db_path: Path | None = None,
    workflow_db_path: Path | None = None,
) -> bool:
    materialized_outbox_ids: set[str] = set()
    try:
        return _finish_transaction(
            conn,
            execution_id=execution_id,
            token=token,
            result=result,
            spec=spec,
            now=now,
            state_db_path=state_db_path,
            workflow_db_path=workflow_db_path,
            materialized_outbox_ids=materialized_outbox_ids,
        )
    except BaseException:
        with wfdb.write_txn(conn):
            conn.execute(
                """
                UPDATE workflow_executions
                   SET claim_lock = NULL, claim_expires = NULL, updated_at = ?
                 WHERE execution_id = ? AND claim_lock = ?
                """,
                (now, execution_id, token),
            )
        _cancel_unclaimed_outboxes(state_db_path, materialized_outbox_ids)
        raise


def _tick(
    *,
    db_path: Path | None = None,
    state_db_path: Path | None = None,
    limit: int = 10,
    now: int | None = None,
    lease_seconds: int = 60,
) -> TickReport:
    """Advance up to limit queued cheap workflow executions. Return structured report."""
    if limit <= 0:
        return TickReport()

    report = TickReport()
    if state_db_path is not None and _profile_path_error(
        workflow_db_path=None,
        state_db_path=state_db_path,
        node_id="terminal_outbox",
    ) is not None:
        # An explicitly selected state database must belong to the active
        # profile. Reject before opening the workflow database so no active
        # workflow, node run, or outbox projection can be touched.
        return report

    tick_now = int(time.time()) if now is None else now
    workflow_db_path = Path(db_path) if db_path is not None else None
    wfdb.init_db(db_path)
    with wfdb.connect(db_path) as conn:
        projection_blocked = _resume_terminal_outbox_nodes(
            conn,
            now=tick_now,
            state_db_path=state_db_path,
            workflow_db_path=workflow_db_path,
        )
        if projection_blocked:
            return report
        _resume_due_waits(conn, now=tick_now)
        _resume_due_retries(conn, now=tick_now)
        _resume_completed_agent_tasks(conn, now=tick_now)
        wfdb.sync_terminal_input_items(conn, now=tick_now)
        while report.processed < limit:
            claimed = _claim_next(conn, now=tick_now, lease_seconds=lease_seconds)
            if claimed is None:
                scheduled = _fire_due_schedules(conn, now=tick_now, limit=limit - report.processed)
                if scheduled:
                    report.schedules_admitted += scheduled
                    continue
                started = _start_ready_feed_items(conn, now=tick_now, limit=1)
                if not started:
                    break
                report.feed_items_admitted += started
                report.processed += started
                continue
            execution_id, token = claimed
            execution = None
            spec = None
            try:
                execution = wfdb.get_execution(conn, execution_id)
                spec = wfdb.get_definition(conn, execution.workflow_id, execution.version)
                send_node_id = next(
                    (
                        node_id
                        for node_id, node in spec.nodes.items()
                        if node.type == "send_message"
                    ),
                    None,
                )
                path_error = (
                    _profile_path_error(
                        workflow_db_path=workflow_db_path,
                        state_db_path=state_db_path,
                        node_id=send_node_id,
                    )
                    if send_node_id is not None
                    else None
                )
                terminal_error = execution.context.get("_terminal_outbox_error")
                if path_error is not None:
                    result = EngineResult(
                        status="failed",
                        context=execution.context,
                        waiting_nodes=[],
                        error={
                            "message": path_error,
                            "node": send_node_id,
                            "phase": "send_message_materialization",
                        },
                    )
                elif isinstance(terminal_error, dict):
                    result = EngineResult(
                        status="failed",
                        context=execution.context,
                        waiting_nodes=[],
                        error=terminal_error,
                    )
                else:
                    completed_wait_nodes = _completed_wait_nodes(conn, execution_id)
                    completed_outputs = _completed_node_outputs(conn, execution_id)
                    kwargs: dict[str, Any] = _catch_resume_kwargs(
                        conn,
                        execution_id=execution_id,
                        context=execution.context,
                        spec=spec,
                    )
                    if completed_wait_nodes:
                        kwargs["completed_wait_nodes"] = completed_wait_nodes
                    if completed_outputs:
                        kwargs["completed_node_outputs"] = completed_outputs
                    result = run_in_memory_until_waiting(spec, execution.input, **kwargs)
            except Exception as exc:
                context = execution.context if execution is not None else {"node": {}}
                result = EngineResult(
                    status="failed",
                    context=context,
                    waiting_nodes=[],
                    error={"message": str(exc)},
                )
            if _finish(
                conn,
                execution_id=execution_id,
                token=token,
                result=result,
                spec=spec,
                now=tick_now,
                state_db_path=state_db_path,
                workflow_db_path=workflow_db_path,
            ):
                report.executions_advanced += 1
                report.processed += 1
        # ponytail: remaining counts via single queries instead of tracking in loop
        remaining = conn.execute(
            "SELECT status FROM workflow_executions WHERE status IN ('queued', 'running', 'waiting')"
        ).fetchall()
        for row in remaining:
            if row["status"] == "queued":
                report.remaining_queued += 1
            else:
                report.remaining_running_or_waiting += 1
        wfdb.sync_terminal_input_items(conn, now=tick_now)
    return report


def tick(
    *,
    db_path: Path | None = None,
    state_db_path: Path | None = None,
    limit: int = 10,
    now: int | None = None,
    lease_seconds: int = 60,
) -> int:
    """Advance up to limit queued cheap workflow executions. Return number processed."""
    return _tick(
        db_path=db_path,
        state_db_path=state_db_path,
        limit=limit,
        now=now,
        lease_seconds=lease_seconds,
    ).processed


def tick_detailed(
    *,
    db_path: Path | None = None,
    state_db_path: Path | None = None,
    limit: int = 10,
    now: int | None = None,
    lease_seconds: int = 60,
) -> TickReport:
    """Advance up to limit queued cheap workflow executions. Return structured report."""
    return _tick(
        db_path=db_path,
        state_db_path=state_db_path,
        limit=limit,
        now=now,
        lease_seconds=lease_seconds,
    )
