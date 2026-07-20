"""Mission aggregate persistence — sits beside workflow_executions in workflows.db.

Missions are a profile-scoped boundary: each mission records its profile
(the active profile at the time of creation), the objective, constraints,
authority scope, and evidence requirements. The link to the workflow
execution that runs the work is kept in ``mission_execution_links`` so
the mission and execution can be created atomically inside one
``write_txn`` — a failing execution rolls back the whole mission
aggregate.
"""

from __future__ import annotations

import copy
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Iterable

from hades_cli import workflows_db as wfdb
from hades_cli.workflows_spec import WorkflowSpec


# Mission status / verdict vocabularies. Anything outside these is
# rejected at the boundary before SQL runs.
_MISSION_STATUSES: frozenset[str] = frozenset({
    "draft",
    "pending_authorization",
    "running",
    "blocked",
    "succeeded",
    "failed",
    "cancelled",
})
_TERMINAL_MISSION_STATUSES: frozenset[str] = frozenset({
    "succeeded",
    "failed",
    "cancelled",
})
_MISSION_VERDICTS: frozenset[str] = frozenset({
    "succeeded",
    "failed",
    "cancelled",
    "abandoned",
    "verified",
    "completed_unverified",
    "unknown_effect",
    "blocked",
})
_RECEIPT_VERDICTS: frozenset[str] = frozenset({
    "failed",
    "blocked",
    "unknown_effect",
    "completed_unverified",
    "verified",
})
# Smallest review-item lifecycle that covers the API exposed today.
# ``pending`` is the default; callers may resolve via ``resolved`` (set
# ``resolved_at`` separately if needed). No speculative taxonomy.
_REVIEW_ITEM_STATUSES: frozenset[str] = frozenset({"pending", "resolved"})


class MissionStateError(ValueError):
    """Invalid mission status/verdict or illegal transition."""


@dataclass(frozen=True)
class MissionRecord:
    mission_id: str
    profile: str
    objective: str
    constraints: list[Any]
    authority: dict[str, Any]
    evidence: dict[str, Any]
    authority_version: int
    status: str
    verdict: str | None
    receipt_id: str | None
    created_at: int
    updated_at: int
    terminal_at: int | None


@dataclass(frozen=True)
class MissionEvent:
    event_id: int
    mission_id: str
    kind: str
    payload: dict[str, Any]
    idempotency_key: str | None
    created_at: int


@dataclass(frozen=True)
class MissionReviewItem:
    review_id: str
    mission_id: str
    transaction_id: str | None
    kind: str
    status: str
    detail: dict[str, Any]
    created_at: int
    resolved_at: int | None


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_loads(value: str | None) -> Any:
    if value is None or value == "":
        return None
    return json.loads(value)


def _new_mission_id() -> str:
    return f"mission_{secrets.token_hex(8)}"


def _validate_inputs(
    *,
    workflow_id: str,
    objective: str,
    constraints: Iterable[Any],
    authority: dict[str, Any],
    evidence: dict[str, Any],
    input_data: dict[str, Any],
    profile: str,
) -> None:
    if not isinstance(workflow_id, str) or not workflow_id.strip():
        raise ValueError("workflow_id is required")
    if not isinstance(objective, str) or not objective.strip():
        raise ValueError("objective is required")
    if not isinstance(constraints, (list, tuple)):
        raise ValueError("constraints must be a list")
    if not isinstance(authority, dict):
        raise ValueError("authority must be a dict")
    if not isinstance(evidence, dict):
        raise ValueError("evidence must be a dict")
    # V1 has a closed evidence vocabulary. Reject an unknown check before
    # opening the mission/execution transaction so no mission can start with
    # a condition the receipt scorer cannot independently evaluate.
    from agent.mission_evidence import validate_evidence_manifest

    validate_evidence_manifest(evidence)
    if not isinstance(input_data, dict):
        raise ValueError("input_data must be a dict")
    if not isinstance(profile, str) or not profile.strip():
        raise ValueError("profile is required")


def _check_agent_task_profile(spec: WorkflowSpec, *, profile: str) -> None:
    """Reject the mission if any agent_task node names a different profile.

    A mission's profile is the active-profile boundary for V1. A workflow
    that dispatches to another profile would violate that boundary.
    """
    for node_id, node in spec.nodes.items():
        if node.type != "agent_task":
            continue
        node_profile = (node.profile or "").strip()
        if node_profile and node_profile != profile:
            raise MissionStateError(
                f"agent_task node {node_id!r} targets profile "
                f"{node_profile!r}; mission profile is {profile!r}"
            )


def _mission_from_row(row: sqlite3.Row) -> MissionRecord:
    return MissionRecord(
        mission_id=row["mission_id"],
        profile=row["profile"],
        objective=row["objective"],
        constraints=copy.deepcopy(_json_loads(row["constraints_json"]) or []),
        authority=copy.deepcopy(_json_loads(row["authority_json"]) or {}),
        evidence=copy.deepcopy(_json_loads(row["evidence_json"]) or {}),
        authority_version=row["authority_version"],
        status=row["status"],
        verdict=row["verdict"],
        receipt_id=row["receipt_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        terminal_at=row["terminal_at"],
    )


def _event_from_row(row: sqlite3.Row) -> MissionEvent:
    return MissionEvent(
        event_id=row["id"],
        mission_id=row["mission_id"],
        kind=row["kind"],
        payload=copy.deepcopy(_json_loads(row["payload_json"]) or {}),
        idempotency_key=row["idempotency_key"],
        created_at=row["created_at"],
    )


def _review_from_row(row: sqlite3.Row) -> MissionReviewItem:
    return MissionReviewItem(
        review_id=row["review_id"],
        mission_id=row["mission_id"],
        transaction_id=row["transaction_id"],
        kind=row["kind"],
        status=row["status"],
        detail=copy.deepcopy(_json_loads(row["detail_json"]) or {}),
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
    )


def create_mission_and_execution(
    conn: sqlite3.Connection,
    *,
    workflow_id: str,
    objective: str,
    constraints: list[Any],
    authority: dict[str, Any],
    evidence: dict[str, Any],
    input_data: dict[str, Any],
    profile: str,
    trigger_type: str = "mission",
    trigger_id: str | None = None,
    now: int | None = None,
    mission_id: str | None = None,
) -> tuple[MissionRecord, wfdb.WorkflowExecution]:
    """Persist a mission + its primary workflow execution in one transaction.

    Validates inputs, rejects cross-profile agent_task workflows BEFORE
    any row is written, then writes mission → execution (via
    ``wfdb.start_execution``) → primary link → two initial events inside
    one outer ``write_txn``. A failure inside any step rolls back the
    whole aggregate.

    Uses the caller's connection only — never opens a second one.
    """
    _validate_inputs(
        workflow_id=workflow_id,
        objective=objective,
        constraints=constraints,
        authority=authority,
        evidence=evidence,
        input_data=input_data,
        profile=profile,
    )

    ts = int(time.time()) if now is None else int(now)
    new_mission_id = mission_id or _new_mission_id()

    with wfdb.write_txn(conn):
        # Read + cross-profile check INSIDE the same write_txn as the
        # inserts so the workflow definition snapshot matches what the
        # rest of the transaction writes against.
        _check_agent_task_profile(
            wfdb.get_definition_record(conn, workflow_id).spec, profile=profile
        )
        conn.execute(
            """
            INSERT INTO missions (
                mission_id, profile, objective,
                constraints_json, authority_json, evidence_json,
                authority_version, status, verdict, receipt_id,
                created_at, updated_at, terminal_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL)
            """,
            (
                new_mission_id,
                profile,
                objective,
                _json_dumps(list(constraints)),
                _json_dumps(authority),
                _json_dumps(evidence),
                1,
                "running",
                ts,
                ts,
            ),
        )
        # Call the canonical start_execution INSIDE our outer write_txn.
        # write_txn composes when conn.in_transaction is already true, so
        # the execution insert joins our transaction. start_execution also
        # re-reads + re-validates the definition (enabled, exists), which
        # is what makes the "disabled workflow" path roll the mission
        # back too.
        execution_id = wfdb.start_execution(
            conn,
            workflow_id,
            input_data=input_data,
            trigger_type=trigger_type,
            trigger_id=trigger_id,
            now=ts,
        )
        conn.execute(
            """
            INSERT INTO mission_execution_links (
                mission_id, execution_id, relation, linked_at
            ) VALUES (?, ?, 'primary', ?)
            """,
            (new_mission_id, execution_id, ts),
        )
        # Two initial events. Created first so the mission timeline
        # starts with intent, then the execution that carries it out.
        _insert_event_in_txn(
            conn,
            mission_id=new_mission_id,
            kind="mission_created",
            payload={"objective": objective, "profile": profile},
            idempotency_key=f"mission_created:{new_mission_id}",
            now=ts,
        )
        _insert_event_in_txn(
            conn,
            mission_id=new_mission_id,
            kind="execution_started",
            payload={"execution_id": execution_id, "workflow_id": workflow_id},
            idempotency_key=f"execution_started:{execution_id}",
            now=ts,
        )

    mission = get_mission(conn, new_mission_id)
    execution = wfdb.get_execution(conn, execution_id)
    return mission, execution


def get_mission(conn: sqlite3.Connection, mission_id: str) -> MissionRecord:
    row = conn.execute(
        "SELECT * FROM missions WHERE mission_id = ?", (mission_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"mission not found: {mission_id}")
    return _mission_from_row(row)


def list_execution_links(
    conn: sqlite3.Connection, mission_id: str
) -> list[str]:
    """Return execution_ids linked to a mission, oldest link first."""
    rows = conn.execute(
        """
        SELECT execution_id FROM mission_execution_links
         WHERE mission_id = ?
         ORDER BY linked_at, execution_id
        """,
        (mission_id,),
    ).fetchall()
    return [row["execution_id"] for row in rows]


def mission_for_execution(
    conn: sqlite3.Connection, execution_id: str
) -> MissionRecord | None:
    """Return the mission linked to *execution_id*, or ``None``.

    Exactly one link is required.  If the database contains more than one
    mission link for the same execution, ``MissionStateError`` is raised
    because the delivery authority must never be selected arbitrarily.
    """
    rows = conn.execute(
        """
        SELECT mission_id FROM mission_execution_links
         WHERE execution_id = ?
         ORDER BY linked_at, mission_id
        """,
        (execution_id,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        ids = [r["mission_id"] for r in rows]
        raise MissionStateError(
            f"execution {execution_id!r} has multiple mission links: {ids}; "
            "delivery authority cannot be determined"
        )
    return get_mission(conn, rows[0]["mission_id"])


def list_mission_events(
    conn: sqlite3.Connection, mission_id: str
) -> list[MissionEvent]:
    rows = conn.execute(
        """
        SELECT id, mission_id, kind, payload_json, idempotency_key, created_at
          FROM mission_events
         WHERE mission_id = ?
         ORDER BY id
        """,
        (mission_id,),
    ).fetchall()
    return [_event_from_row(row) for row in rows]  # ponytail: renames iter_ → list_; materializes


def append_mission_event(
    conn: sqlite3.Connection,
    mission_id: str,
    *,
    kind: str,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    now: int | None = None,
) -> MissionEvent:
    """Append an event. ``idempotency_key`` makes repeats return the same row.

    Requires ``missions.id`` to exist; keying on ``(mission_id,
    idempotency_key)`` lets safe retries converge to a single event.
    No-key calls are append-only: SQLite's UNIQUE constraint treats
    NULLs as distinct, so each no-key append creates a new row.
    """
    if not isinstance(kind, str) or not kind.strip():
        raise MissionStateError("event kind is required")
    ts = int(time.time()) if now is None else int(now)
    with wfdb.write_txn(conn):
        try:
            cur = conn.execute(
                """
                INSERT INTO mission_events (
                    mission_id, kind, payload_json, idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (mission_id, kind, _json_dumps(payload or {}), idempotency_key, ts),
            )
        except sqlite3.IntegrityError:
            # Duplicate (mission_id, idempotency_key); NULL keys never collide.
            row = conn.execute(
                """
                SELECT id, mission_id, kind, payload_json, idempotency_key, created_at
                  FROM mission_events
                 WHERE mission_id = ? AND idempotency_key = ?
                """,
                (mission_id, idempotency_key),
            ).fetchone()
            if row is None:
                raise
            return _event_from_row(row)
        event_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    return MissionEvent(
        event_id=event_id,
        mission_id=mission_id,
        kind=kind,
        payload=copy.deepcopy(payload or {}),
        idempotency_key=idempotency_key,
        created_at=ts,
    )


def _insert_event_in_txn(
    conn: sqlite3.Connection,
    *,
    mission_id: str,
    kind: str,
    payload: dict[str, Any],
    idempotency_key: str | None,
    now: int,
) -> None:
    """Insert an event row inside the caller's transaction.

    Used by ``create_mission_and_execution`` so its initial events share
    the same write_txn as the mission and execution inserts.
    """
    conn.execute(
        """
        INSERT INTO mission_events (
            mission_id, kind, payload_json, idempotency_key, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (mission_id, kind, _json_dumps(payload or {}), idempotency_key, now),
    )


def upsert_review_item(
    conn: sqlite3.Connection,
    mission_id: str,
    *,
    review_id: str,
    kind: str,
    detail: dict[str, Any] | None = None,
    status: str = "pending",
    transaction_id: str | None = None,
    now: int | None = None,
) -> MissionReviewItem:
    """Create-or-return a review item keyed by ``review_id``."""
    if not isinstance(review_id, str) or not review_id.strip():
        raise MissionStateError("review_id is required")
    if not isinstance(kind, str) or not kind.strip():
        raise MissionStateError("review kind is required")
    if not isinstance(status, str) or not status.strip():
        raise MissionStateError("review status is required")
    if status not in _REVIEW_ITEM_STATUSES:
        raise MissionStateError(f"invalid review status: {status!r}")
    ts = int(time.time()) if now is None else int(now)
    with wfdb.write_txn(conn):
        row = conn.execute(
            "SELECT * FROM mission_review_items WHERE review_id = ?",
            (review_id,),
        ).fetchone()
        if row is not None:
            return _review_from_row(row)
        conn.execute(
            """
            INSERT INTO mission_review_items (
                review_id, mission_id, transaction_id, kind, status,
                detail_json, created_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                review_id,
                mission_id,
                transaction_id,
                kind,
                status,
                _json_dumps(detail or {}),
                ts,
            ),
        )
    row = conn.execute(
        "SELECT * FROM mission_review_items WHERE review_id = ?", (review_id,)
    ).fetchone()
    return _review_from_row(row)


def set_mission_status(
    conn: sqlite3.Connection,
    mission_id: str,
    status: str,
    *,
    now: int | None = None,
) -> MissionRecord:
    """Move a mission's status forward. Terminal status/verdict blocks rewrites."""
    if status not in _MISSION_STATUSES:
        raise MissionStateError(f"invalid mission status: {status!r}")
    mission = get_mission(conn, mission_id)
    already_terminal = mission.status in _TERMINAL_MISSION_STATUSES
    already_verdicted = mission.verdict in _MISSION_VERDICTS
    # Terminal statuses are final — the only legal update from this state
    # is the same-value idempotent write. Same for a verdicted mission: no
    # status flip can resurrect it.
    if already_terminal and status != mission.status:
        raise MissionStateError(
            f"mission {mission_id} has terminal status {mission.status!r}; "
            f"cannot transition to {status!r}"
        )
    if already_verdicted and not already_terminal:
        raise MissionStateError(
            f"mission {mission_id} has terminal verdict {mission.verdict!r}; "
            f"cannot change status to {status!r}"
        )
    ts = int(time.time()) if now is None else int(now)
    # Sticky terminal_at: never overwrite the first terminalization timestamp.
    if mission.terminal_at is not None:
        terminal_at = mission.terminal_at
    else:
        terminal_at = ts if status in _TERMINAL_MISSION_STATUSES else None
    with wfdb.write_txn(conn):
        conn.execute(
            """
            UPDATE missions
               SET status = ?, updated_at = ?, terminal_at = ?
             WHERE mission_id = ?
            """,
            (status, ts, terminal_at, mission_id),
        )
    return get_mission(conn, mission_id)


def project_receipt_verdict(
    conn: sqlite3.Connection,
    mission_id: str,
    *,
    receipt_id: str,
    verdict: str,
    now: int | None = None,
) -> MissionRecord:
    """Link one already-persisted receipt to its terminal mission projection.

    The receipt row is intentionally external to ``workflows.db``.  This
    projection is therefore CAS-style and idempotent: a restart can link the
    same deterministic receipt, but cannot replace a different receipt or
    retroactively revise a terminal evidence decision.
    """
    if not isinstance(receipt_id, str) or not receipt_id:
        raise MissionStateError("receipt_id is required")
    if verdict not in _RECEIPT_VERDICTS:
        raise MissionStateError(f"invalid receipt verdict: {verdict!r}")
    ts = int(time.time()) if now is None else int(now)
    with wfdb.write_txn(conn):
        cursor = conn.execute(
            """
            UPDATE missions
               SET receipt_id = ?, verdict = ?, updated_at = ?,
                   terminal_at = COALESCE(terminal_at, ?)
             WHERE mission_id = ?
               AND (receipt_id IS NULL OR receipt_id = ?)
               AND (verdict IS NULL OR verdict = ?)
            """,
            (receipt_id, verdict, ts, ts, mission_id, receipt_id, verdict),
        )
        if cursor.rowcount != 1:
            current = get_mission(conn, mission_id)
            if current.receipt_id not in {None, receipt_id}:
                raise MissionStateError(
                    f"mission {mission_id} already links receipt {current.receipt_id!r}"
                )
            raise MissionStateError(
                f"mission {mission_id} already has terminal verdict {current.verdict!r}"
            )
    return get_mission(conn, mission_id)


def set_mission_verdict(
    conn: sqlite3.Connection,
    mission_id: str,
    verdict: str,
    *,
    now: int | None = None,
) -> MissionRecord:
    """Record a final verdict on a mission. Every admitted verdict is final."""
    if verdict not in _MISSION_VERDICTS:
        raise MissionStateError(f"invalid mission verdict: {verdict!r}")
    mission = get_mission(conn, mission_id)
    # A recorded verdict is final — same-value idempotence is allowed,
    # but no transition to a different terminal verdict.
    if mission.verdict in _MISSION_VERDICTS and verdict != mission.verdict:
        raise MissionStateError(
            f"mission {mission_id} already has terminal verdict "
            f"{mission.verdict!r}; cannot transition to {verdict!r}"
        )
    ts = int(time.time()) if now is None else int(now)
    # Sticky terminal_at: never overwrite the first terminalization timestamp.
    if mission.terminal_at is not None:
        terminal_at = mission.terminal_at
    else:
        terminal_at = ts if verdict in _MISSION_VERDICTS else None
    with wfdb.write_txn(conn):
        conn.execute(
            """
            UPDATE missions
               SET verdict = ?, updated_at = ?, terminal_at = ?
             WHERE mission_id = ?
            """,
            (verdict, ts, terminal_at, mission_id),
        )
    return get_mission(conn, mission_id)