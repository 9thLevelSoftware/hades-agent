"""Durable operation journal backed by the profile-local state database.

This is a library primitive; production integration and startup reconciliation
belong to higher-level callers.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

from hermes_state import SessionDB


def _process_start_time(pid: int) -> Optional[int]:
    try:
        from gateway.status import get_process_start_time

        return get_process_start_time(pid)
    except Exception:
        return None


def _owner_is_live(pid: Optional[int], started_at: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        from gateway.status import _pid_exists, get_process_start_time

        if not _pid_exists(pid):
            return False
        current_start = get_process_start_time(pid)
        return started_at is None or current_start is None or current_start == started_at
    except Exception:
        # gateway.status is the authoritative cross-platform PID probe. If it
        # is unavailable, fail closed rather than using os.kill(pid, 0), which
        # has destructive console semantics on Windows.
        return False


_STATES = frozenset(
    {"pending", "running", "dispatched", "confirmed", "failed", "unknown", "cancelled"}
)
_EFFECTS = frozenset({"none", "landed", "unknown"})
_EFFECTS_BY_STATE = {
    "pending": frozenset({"none"}),
    "running": frozenset({"none"}),
    "dispatched": frozenset({"unknown"}),
    "confirmed": frozenset({"none", "landed"}),
    "failed": frozenset({"none", "unknown"}),
    "unknown": frozenset({"unknown"}),
    "cancelled": frozenset({"none"}),
}
_TERMINAL_STATES = frozenset({"confirmed", "failed", "unknown", "cancelled"})
_TRANSITIONS = {
    "pending": frozenset({"running", "cancelled", "failed"}),
    "running": frozenset({"dispatched", "confirmed", "failed", "unknown", "cancelled"}),
    "dispatched": frozenset({"confirmed", "failed", "unknown"}),
    "confirmed": frozenset(),
    "failed": frozenset({"running"}),
    "unknown": frozenset(),
    "cancelled": frozenset(),
}


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    kind: str
    session_id: str
    turn_id: str
    tool_call_id: str
    destination: str
    payload_hash: str
    state: str
    effect_disposition: str
    result_json: Optional[str]
    error: Optional[str]
    created_at: float
    updated_at: float
    acknowledged_at: Optional[float]


class OperationJournal:
    """Record operation certainty without making uncertain work retryable."""

    def __init__(self, db: SessionDB):
        self._db = db

    @staticmethod
    def _record(row) -> OperationRecord:
        return OperationRecord(**dict(row))

    @staticmethod
    def _result_json(result: Any) -> Optional[str]:
        return None if result is None else json.dumps(result, sort_keys=True, default=str)

    @staticmethod
    def _validate_transition(
        from_states: set[str], to_state: str, effect_disposition: str
    ) -> None:
        if not from_states or not from_states <= _STATES:
            raise ValueError("invalid operation source state")
        if to_state not in _STATES:
            raise ValueError("invalid operation target state")
        if effect_disposition not in _EFFECTS:
            raise ValueError("invalid operation effect disposition")
        if effect_disposition not in _EFFECTS_BY_STATE[to_state]:
            raise ValueError("invalid operation state/effect pair")
        if any(to_state not in _TRANSITIONS[state] for state in from_states):
            raise ValueError("invalid operation state transition")

    def create(
        self,
        *,
        operation_id: str,
        kind: str,
        session_id: str = "",
        turn_id: str = "",
        tool_call_id: str = "",
        destination: str = "",
        payload_hash: str = "",
    ) -> OperationRecord:
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("operation_id must be a non-empty string")
        if not isinstance(kind, str) or not kind:
            raise ValueError("kind must be a non-empty string")
        identity = (
            kind,
            session_id,
            turn_id,
            tool_call_id,
            destination,
            payload_hash,
        )

        def _create(conn):
            row = conn.execute(
                "SELECT * FROM agent_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is not None:
                existing_identity = tuple(row[field] for field in (
                    "kind",
                    "session_id",
                    "turn_id",
                    "tool_call_id",
                    "destination",
                    "payload_hash",
                ))
                if existing_identity != identity:
                    raise ValueError(
                        f"operation id {operation_id!r} already identifies a different operation"
                    )
                return row

            now = time.time()
            conn.execute(
                """INSERT INTO agent_operations (
                       operation_id, kind, session_id, turn_id, tool_call_id,
                       destination, payload_hash, state, effect_disposition,
                       result_json, error, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 'none', NULL, NULL, ?, ?)""",
                (
                    operation_id,
                    kind,
                    session_id,
                    turn_id,
                    tool_call_id,
                    destination,
                    payload_hash,
                    now,
                    now,
                ),
            )
            conn.execute(
                """INSERT INTO agent_operation_owners (
                       operation_id, owner_pid, owner_started_at
                   ) VALUES (?, ?, ?)""",
                (operation_id, os.getpid(), _process_start_time(os.getpid())),
            )
            return conn.execute(
                "SELECT * FROM agent_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()

        return self._record(self._db._execute_write(_create))

    def transition(
        self,
        operation_id: str,
        *,
        from_states: set[str],
        to_state: str,
        effect_disposition: str,
        result: Any = None,
        error: Optional[str] = None,
    ) -> OperationRecord:
        self._validate_transition(from_states, to_state, effect_disposition)
        placeholders = ",".join("?" for _ in from_states)
        result_json = self._result_json(result)

        def _transition(conn):
            current = conn.execute(
                "SELECT * FROM agent_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if current is None:
                raise KeyError(operation_id)
            if current["state"] not in from_states:
                raise ValueError("stale operation state")

            now = time.time()
            cursor = conn.execute(
                f"""UPDATE agent_operations
                       SET state = ?, effect_disposition = ?, result_json = ?,
                           error = ?, updated_at = ?
                     WHERE operation_id = ? AND state IN ({placeholders})""",
                (
                    to_state,
                    effect_disposition,
                    result_json,
                    error,
                    now,
                    operation_id,
                    *from_states,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("stale operation state")
            return conn.execute(
                "SELECT * FROM agent_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()

        return self._record(self._db._execute_write(_transition))

    def acknowledge(self, operation_id: str) -> bool:
        placeholders = ",".join("?" for _ in _TERMINAL_STATES)

        def _acknowledge(conn):
            cursor = conn.execute(
                f"""UPDATE agent_operations
                       SET acknowledged_at = ?
                     WHERE operation_id = ?
                       AND state IN ({placeholders})
                       AND acknowledged_at IS NULL""",
                (time.time(), operation_id, *_TERMINAL_STATES),
            )
            return cursor.rowcount == 1

        return self._db._execute_write(_acknowledge)

    def get(self, operation_id: str) -> Optional[OperationRecord]:
        def _read(conn):
            return conn.execute(
                "SELECT * FROM agent_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()

        row = self._db._execute_read(_read)
        return self._record(row) if row is not None else None

    def list_unacknowledged(self, kind: Optional[str] = None) -> list[OperationRecord]:
        query = "SELECT * FROM agent_operations WHERE acknowledged_at IS NULL"
        params: list[str] = []
        if kind is not None:
            query += " AND kind = ?"
            params.append(kind)
        query += " ORDER BY created_at, operation_id"

        def _read(conn):
            return conn.execute(query, params).fetchall()

        rows = self._db._execute_read(_read)
        return [self._record(row) for row in rows]

    def reconcile_after_restart(self, *, owner_fenced: bool = False) -> int:
        if not owner_fenced:
            def _reconcile(conn):
                cursor = conn.execute(
                    """UPDATE agent_operations
                           SET state = 'unknown', effect_disposition = 'unknown', updated_at = ?
                         WHERE state IN ('running', 'dispatched')""",
                    (time.time(),),
                )
                return cursor.rowcount

            return self._db._execute_write(_reconcile)

        def _reconcile_fenced(conn):
            rows = conn.execute(
                """SELECT operations.operation_id, owners.owner_pid, owners.owner_started_at
                     FROM agent_operations AS operations
                LEFT JOIN agent_operation_owners AS owners
                       ON owners.operation_id = operations.operation_id
                    WHERE operations.state IN ('running', 'dispatched')"""
            ).fetchall()
            stale_ids = [
                row["operation_id"]
                for row in rows
                if not _owner_is_live(row["owner_pid"], row["owner_started_at"])
            ]
            if not stale_ids:
                return 0
            placeholders = ",".join("?" for _ in stale_ids)
            cursor = conn.execute(
                f"""UPDATE agent_operations
                       SET state = 'unknown', effect_disposition = 'unknown', updated_at = ?
                     WHERE operation_id IN ({placeholders})
                       AND state IN ('running', 'dispatched')""",
                (time.time(), *stale_ids),
            )
            return cursor.rowcount

        return self._db._execute_write(_reconcile_fenced)

    def prune_terminal(self, older_than_days: float = 30) -> int:
        """Delete acknowledged confirmed/failed/cancelled rows older than cutoff.

        Bounded retention: only rows that are *all of* terminal (confirmed,
        failed, or cancelled — never ``unknown``), *acknowledged*, and
        ``created_at < now - older_than_days`` are removed. Pending,
        running, dispatched, unknown, and unacknowledged records are
        preserved unconditionally so we never lose evidence of in-flight or
        un-reconciled work. Returns the number of rows deleted.
        """
        cutoff = time.time() - older_than_days * 86400

        def _prune(conn):
            cursor = conn.execute(
                """DELETE FROM agent_operations
                    WHERE state IN ('confirmed', 'failed', 'cancelled')
                      AND acknowledged_at IS NOT NULL
                      AND created_at < ?""",
                (cutoff,),
            )
            return cursor.rowcount

        return self._db._execute_write(_prune)
