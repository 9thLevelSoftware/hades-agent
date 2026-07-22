#!/usr/bin/env python3
"""
Async (background) delegation registry.

Backs ``delegate_task(background=true)``: the parent agent dispatches a
subagent that runs on a module-level daemon executor and returns a handle
immediately, so the user and the model can keep working while the child runs.

When the child finishes, a completion event is pushed onto the SHARED
``process_registry.completion_queue`` with ``type="async_delegation"``. The
CLI (``cli.py`` process_loop) and gateway (``_run_process_watcher`` /
``completion_queue`` drain) already poll that queue while the agent is idle
and forge a fresh user/internal turn from each event. We deliberately reuse
that rail rather than reaching into a running agent loop:

  - completions surface as a NEW turn when the agent is idle, never spliced
    between a tool result and an assistant message. That keeps strict
    message-role alternation legal and the prompt cache intact (hard
    invariant: never mutate past context).
  - we inherit the queue's de-dup, crash-recovery checkpoint, and the
    existing CLI + gateway drain wiring for free — no new drain loops in the
    two largest files in the repo.

The completion payload carries a RICH, self-contained task-source block (the
original goal, the context the parent supplied, toolsets, model, dispatch
time, status, and the full result summary). When the result re-enters the
conversation the parent may be deep in unrelated context and won't remember
why the subagent existed; the block lets it either use the result or
re-dispatch if the world has moved on.

This module owns ONLY the async lifecycle. The actual child build + run is
delegated back to ``delegate_tool._run_single_child`` via an injected
runner, so all the credential leasing, heartbeat, timeout, and result-shaping
logic stays in one place.

Dispatch metadata is also written atomically to ``delegations.json`` under the
active profile. After a process restart, records that were still running are
reported as ``interrupted`` (never falsely resumed), with their original goal,
context, and child session IDs preserved. ``/agents`` can then point at the
child's existing ``/resume`` path without re-running side effects blindly.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from hermes_constants import get_hermes_home
from tools.daemon_pool import DaemonThreadPoolExecutor
from tools.thread_context import propagate_context_to_thread

logger = logging.getLogger(__name__)

# Back-compat alias — the daemon executor now lives in tools.daemon_pool so
# other subsystems (tool_executor, memory_manager, delegate_tool, skills_hub)
# can share it. Existing imports of ``_DaemonThreadPoolExecutor`` keep working.
_DaemonThreadPoolExecutor = DaemonThreadPoolExecutor


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
# A persistent daemon executor (NOT a `with ThreadPoolExecutor()` block, which
# would join on exit and defeat the whole point of async). Workers are daemon
# threads so a hard process exit doesn't hang on an in-flight child.
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
_executor_max_workers: int = 0

_records_lock = threading.Lock()
# delegation_id -> record dict. Kept for the lifetime of the run plus a short
# tail after completion so `list_async_delegations()` can show recent results.
_records: Dict[str, Dict[str, Any]] = {}

_DEFAULT_MAX_ASYNC_CHILDREN = 3
# How many completed records to retain for status queries before pruning.
_MAX_RETAINED_COMPLETED = 50
_DURABLE_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_DURABLE_PENDING = 1000
# A pending completion whose delivery keeps failing is retried across claim
# cycles (and across restarts via restore_undelivered_completions). Cap the
# attempts so an unroutable row converges to a terminal 'dropped' state
# instead of replaying on every restart forever.
_MAX_DELIVERY_ATTEMPTS = 8
_DB_LOCK = threading.Lock()

# Durable operation journal is cached per Hermes profile, not process-wide.
# ponytail: one lazy journal per profile avoids cross-profile state bleed without
# adding a broader lifecycle manager.
_journal_lock = threading.Lock()
_journal_cache: Dict[str, Any] = {}
_journal_override: Any = None
_journal_override_enabled = False
_restored_ids: set[str] = set()
_restored_lock = threading.Lock()
_KIND = "async_delegation"
_STATUS_MAP = {
    "completed": ("confirmed", "none"),
    "success": ("confirmed", "none"),
    "error": ("failed", "none"),
    "failed": ("failed", "none"),
    "interrupted": ("cancelled", "none"),
    "cancelled": ("cancelled", "none"),
}


def _open_journal():
    if _journal_override_enabled:
        return _journal_override
    profile = str(get_hermes_home())
    with _journal_lock:
        if profile in _journal_cache:
            return _journal_cache[profile]
        try:
            from agent.operation_journal import OperationJournal
            from hermes_state import SessionDB

            journal = OperationJournal(SessionDB())
        except Exception as exc:
            logger.warning(
                "Async delegation durable journal unavailable for %s: %s",
                profile,
                exc,
            )
            journal = None
        _journal_cache[profile] = journal
        return journal


def _set_journal_for_tests(journal) -> None:
    global _journal_override, _journal_override_enabled, _journal_cache
    with _journal_lock:
        if journal is None:
            _journal_override = None
            _journal_override_enabled = False
            _journal_cache = {}
        else:
            _journal_override = journal
            _journal_override_enabled = True


def _journal_for_tests():
    return _journal_override if _journal_override_enabled else _open_journal()


def _terminal_state_for(status: str) -> tuple[str, str]:
    return _STATUS_MAP.get(status, ("unknown", "unknown"))


def _persist_operation_dispatch(record: Dict[str, Any]) -> None:
    journal = _open_journal()
    if journal is None:
        return
    try:
        delegation_id = record["delegation_id"]
        journal.create(
            operation_id=delegation_id,
            kind=_KIND,
            session_id=record.get("session_key", "") or "",
            destination=record.get("parent_session_id", "") or "",
            payload_hash=f"{delegation_id}|{record['dispatched_at']:.6f}",
        )
        journal.transition(
            delegation_id,
            from_states={"pending"},
            to_state="running",
            effect_disposition="none",
        )
    except Exception as exc:
        logger.debug("Async delegation %s journal dispatch failed: %s", record.get("delegation_id"), exc)


def _persist_operation_completion(event: Dict[str, Any]) -> None:
    journal = _open_journal()
    if journal is None:
        return
    status = str(event.get("status") or "unknown")
    to_state, effect = _terminal_state_for(status)
    try:
        from_states = {"running"} if to_state == "cancelled" else {"running", "dispatched"}
        journal.transition(
            str(event["delegation_id"]),
            from_states=from_states,
            to_state=to_state,
            effect_disposition=effect,
            result=event,
            error=str(event.get("error")) if event.get("error") else None,
        )
    except Exception as exc:
        logger.debug("Async delegation %s journal completion failed: %s", event.get("delegation_id"), exc)


def acknowledge_async_delegation(operation_id: str) -> bool:
    journal = _open_journal()
    if journal is None:
        return False
    try:
        return bool(journal.acknowledge(operation_id))
    except Exception as exc:
        logger.debug("Async delegation %s journal acknowledge failed: %s", operation_id, exc)
        return False


def _record_to_event(record) -> Optional[Dict[str, Any]]:
    if not record.result_json:
        return None
    try:
        event = json.loads(record.result_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(event, dict):
        return None
    event.setdefault("type", "async_delegation")
    event.setdefault("delegation_id", record.operation_id)
    return event


def restore_unacknowledged_delegations(queue, put_fn) -> int:
    journal = _open_journal()
    if journal is None:
        return 0
    try:
        records = journal.list_unacknowledged(kind=_KIND)
    except Exception as exc:
        logger.warning("Async delegation restore failed: %s", exc)
        return 0
    restored = 0
    with _restored_lock:
        for record in records:
            if record.operation_id in _restored_ids:
                continue
            event = _record_to_event(record)
            if event is None:
                continue
            try:
                put_fn(event)
            except Exception as exc:
                logger.warning("Async delegation restore enqueue failed: %s", exc)
                continue
            _restored_ids.add(record.operation_id)
            restored += 1
    return restored


def _db_path():
    return get_hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS async_delegations (
            delegation_id TEXT PRIMARY KEY,
            origin_session TEXT NOT NULL,
            origin_ui_session_id TEXT NOT NULL DEFAULT '',
            parent_session_id TEXT,
            state TEXT NOT NULL,
            dispatched_at REAL NOT NULL,
            completed_at REAL,
            updated_at REAL NOT NULL,
            event_json TEXT,
            result_json TEXT,
            delivery_state TEXT NOT NULL DEFAULT 'pending',
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            delivered_at REAL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            task_json TEXT,
            delivery_claim TEXT,
            delivery_claimed_at REAL
        )"""
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(async_delegations)")}
    for name, sql_type in (
        ("owner_pid", "INTEGER"),
        ("owner_started_at", "INTEGER"),
        ("task_json", "TEXT"),
        ("delivery_claim", "TEXT"),
        ("delivery_claimed_at", "REAL"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE async_delegations ADD COLUMN {name} {sql_type}")
    return conn


def _persist_dispatch(record: Dict[str, Any]) -> None:
    now = time.time()
    try:
        from gateway.status import get_process_start_time
        owner_started_at = get_process_start_time(__import__("os").getpid())
    except Exception:
        owner_started_at = None
    task_payload = {
        key: record.get(key)
        for key in ("goal", "goals", "context", "toolsets", "role", "model", "is_batch")
        if key in record
    }
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO async_delegations
               (delegation_id, origin_session, origin_ui_session_id,
                parent_session_id, state, dispatched_at, updated_at,
                delivery_state, delivery_attempts, owner_pid,
                owner_started_at, task_json)
               VALUES (?, ?, ?, ?, 'running', ?, ?, 'pending', 0, ?, ?, ?)""",
            (record["delegation_id"], record.get("session_key", ""),
             record.get("origin_ui_session_id", ""), record.get("parent_session_id"),
             record["dispatched_at"], now, __import__("os").getpid(),
             owner_started_at, json.dumps(task_payload)),
        )
    _persist_operation_dispatch(record)
    _prune_durable_records()


def _delete_durable_delegation(delegation_id: str) -> None:
    with _DB_LOCK, _connect() as conn:
        conn.execute("DELETE FROM async_delegations WHERE delegation_id=?", (delegation_id,))


def _prune_durable_records() -> None:
    """Bound terminal history, preferring delivered records for deletion."""
    now = time.time()
    cutoff = now - _DURABLE_RETENTION_SECONDS
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            "DELETE FROM async_delegations WHERE delivery_state='delivered' AND updated_at < ?",
            (cutoff,),
        )
        terminal_count = conn.execute(
            "SELECT COUNT(*) FROM async_delegations WHERE state NOT IN ('running','finalizing')"
        ).fetchone()[0]
        excess = max(0, terminal_count - _MAX_RETAINED_COMPLETED)
        if excess:
            conn.execute(
                """DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE state NOT IN ('running','finalizing')
                     ORDER BY CASE delivery_state WHEN 'delivered' THEN 0 ELSE 1 END,
                              updated_at ASC LIMIT ?
                   )""",
                (excess,),
            )
        pending_count = conn.execute(
            """SELECT COUNT(*) FROM async_delegations
               WHERE state NOT IN ('running','finalizing') AND delivery_state='pending'"""
        ).fetchone()[0]
        overflow = max(0, pending_count - _MAX_DURABLE_PENDING)
        if overflow:
            conn.execute(
                """DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE state NOT IN ('running','finalizing') AND delivery_state='pending'
                     ORDER BY updated_at ASC LIMIT ?
                   )""",
                (overflow,),
            )


def _persist_completion(event: Dict[str, Any], result: Dict[str, Any]) -> None:
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            """UPDATE async_delegations SET state=?, completed_at=?, updated_at=?,
               event_json=?, result_json=?, delivery_state='pending'
               WHERE delegation_id=?""",
            (event.get("status", "completed"), event.get("completed_at", now), now,
             json.dumps(event), json.dumps(result), event["delegation_id"]),
        )
    _persist_operation_completion(event)


def _note_delivery_attempt(delegation_id: str) -> None:
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            "UPDATE async_delegations SET delivery_attempts=delivery_attempts+1, updated_at=? WHERE delegation_id=?",
            (time.time(), delegation_id),
        )


def recover_abandoned_delegations() -> int:
    """Classify records whose owning process disappeared as outcome unknown."""
    try:
        from gateway.status import _pid_exists, get_process_start_time
    except Exception:
        return 0
    now = time.time()
    recovered = 0
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT delegation_id, origin_session, origin_ui_session_id,
                      parent_session_id, dispatched_at, owner_pid,
                      owner_started_at, task_json
               FROM async_delegations WHERE state IN ('running','finalizing')"""
        ).fetchall()
        for row in rows:
            delegation_id, session_key, origin_ui, parent_id, dispatched_at, pid, started, task_json = row
            live = False
            if pid:
                live = _pid_exists(int(pid))
                if live and started is not None:
                    live = get_process_start_time(int(pid)) == int(started)
            if live:
                continue
            task = json.loads(task_json or "{}")
            event = {
                "type": "async_delegation", "delegation_id": delegation_id,
                "session_key": session_key, "origin_ui_session_id": origin_ui,
                "parent_session_id": parent_id, "goal": task.get("goal", ""),
                "goals": task.get("goals"), "context": task.get("context"),
                "toolsets": task.get("toolsets"), "role": task.get("role"),
                "model": task.get("model"), "is_batch": bool(task.get("is_batch")),
                "status": "unknown", "summary": None,
                "error": "Delegation owner exited before recording a terminal result; outcome unknown.",
                "dispatched_at": dispatched_at, "completed_at": now,
            }
            result = {"status": "unknown", "summary": None, "error": event["error"]}
            conn.execute(
                """UPDATE async_delegations SET state='unknown', completed_at=?,
                   updated_at=?, event_json=?, result_json=?, delivery_state='pending'
                   WHERE delegation_id=?""",
                (now, now, json.dumps(event), json.dumps(result), delegation_id),
            )
            recovered += 1
    return recovered


def restore_undelivered_completions(target_queue) -> int:
    """Enqueue durable pending completions as fresh turns after process start.

    Every restored event is stamped ``restored=True`` (in-memory only — the
    stamp is added after the durable payload is deserialized and is never
    persisted). Restored events originate from a *previous* process, so no
    consumer in THIS process implicitly owns them: drain paths that run
    without an ownership filter (the legacy single-session behavior) must
    leave them queued for a consumer that can positively prove ownership,
    otherwise a brand-new session adopts a dead session's delegation
    results seconds after boot (#64484).
    """
    recover_abandoned_delegations()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT delegation_id, event_json FROM async_delegations
               WHERE state != 'running' AND delivery_state='pending' AND event_json IS NOT NULL
               ORDER BY completed_at, delegation_id"""
        ).fetchall()
        for _delegation_id, payload in rows:
            evt = json.loads(payload)
            if isinstance(evt, dict):
                evt["restored"] = True
            target_queue.put(evt)
    return len(rows)


def mark_completion_delivered(delegation_id: str) -> bool:
    """Atomically acknowledge successful injection of a durable completion."""
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='delivered', delivered_at=?, updated_at=?
               WHERE delegation_id=? AND delivery_state!='delivered'""",
            (now, now, delegation_id),
        )
        return cur.rowcount == 1


def claim_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Claim one pending completion across competing consumers/processes."""
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT delivery_state FROM async_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
        if row is None:
            return True  # legacy event created before durable dispatch
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_claim=?, delivery_claimed_at=?,
                      delivery_attempts=delivery_attempts+1, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND (delivery_claim IS NULL OR delivery_claimed_at < ?)""",
            (claim_id, now, now, delegation_id, now - 300),
        )
        return cur.rowcount == 1


def claim_event_delivery(evt: Dict[str, Any], consumer: str) -> Optional[str]:
    """Claim a durable delegation event; non-durable events need no token."""
    if evt.get("type") != "async_delegation":
        return ""
    delegation_id = str(evt.get("delegation_id") or "")
    if not delegation_id:
        return ""
    claim_id = f"{consumer}:{__import__('os').getpid()}:{uuid.uuid4().hex}"
    return claim_id if claim_completion_delivery(delegation_id, claim_id) else None


def release_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Release a failed delivery claim so another consumer may retry.

    Attempts are counted at claim time, so a row that keeps being claimed and
    released has burned real delivery attempts. Once the budget is exhausted
    the row converges to a terminal ``dropped`` state instead of returning to
    ``pending`` — otherwise an undeliverable completion replays on every
    gateway restart forever (restore_undelivered_completions only restores
    pending rows).
    """
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        capped = conn.execute(
            """UPDATE async_delegations SET delivery_state='dropped',
                      delivery_claim=NULL, delivery_claimed_at=NULL, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=? AND delivery_attempts>=?""",
            (now, delegation_id, claim_id, _MAX_DELIVERY_ATTEMPTS),
        )
        if capped.rowcount == 1:
            logger.warning(
                "Async delegation %s exhausted its %d delivery attempts; "
                "marking terminally dropped (result remains queryable).",
                delegation_id, _MAX_DELIVERY_ATTEMPTS,
            )
            return True
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_claim=NULL,
                      delivery_claimed_at=NULL, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""",
            (now, delegation_id, claim_id),
        )
        return cur.rowcount == 1


def drop_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Terminally drop a claimed completion that can never be delivered.

    Used when the delivery target is permanently gone — the spawning session
    ended at an explicit user boundary (/new, reset) rather than a compression
    rotation. Marking the row ``dropped`` (not ``delivered``) keeps the ack
    honest, and (not ``pending``) keeps restart recovery from replaying a
    completion that will be fail-closed dropped again every time.
    """
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='dropped',
                      updated_at=?, delivery_claim=NULL,
                      delivery_claimed_at=NULL
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""",
            (now, delegation_id, claim_id),
        )
        return cur.rowcount == 1


def complete_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Acknowledge acceptance for the consumer holding this claim."""
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='delivered',
                      delivered_at=?, updated_at=?, delivery_claim=NULL,
                      delivery_claimed_at=NULL
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""",
            (now, now, delegation_id, claim_id),
        )
        return cur.rowcount == 1


def complete_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "async_delegation":
        complete_completion_delivery(str(evt.get("delegation_id") or ""), claim_id)


def release_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "async_delegation":
        release_completion_delivery(str(evt.get("delegation_id") or ""), claim_id)


def get_durable_delegation(delegation_id: str) -> Optional[Dict[str, Any]]:
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            """SELECT origin_session, state, dispatched_at, completed_at,
                      result_json, delivery_state, delivery_attempts
               FROM async_delegations WHERE delegation_id=?""", (delegation_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "delegation_id": delegation_id, "origin_session": row[0], "state": row[1],
        "dispatched_at": row[2], "completed_at": row[3],
        "result": json.loads(row[4]) if row[4] else None,
        "delivery_state": row[5], "delivery_attempts": row[6],
    }


def _records_path():
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "delegations.json"


def _pid_alive(pid: Any, process_start_time: Any = None) -> bool:
    from hermes_cli.active_sessions import _pid_alive as _active_session_pid_alive

    return _active_session_pid_alive(pid, process_start_time)


def _current_process_start_time() -> Optional[float]:
    from hermes_cli.active_sessions import _process_start_time

    return _process_start_time(os.getpid())


def _read_persisted_records(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        return {}
    records = payload.get("records", [])
    if not isinstance(records, list):
        return {}
    return {
        str(record["delegation_id"]): dict(record)
        for record in records
        if isinstance(record, dict) and record.get("delegation_id")
    }


def _persist_records_locked(
    home: Optional[str] = None,
    *,
    remove_ids: Optional[set[str]] = None,
) -> bool:
    """Atomically persist one profile's records; never break completion delivery."""
    from utils import atomic_json_write

    home = home or str(_records_path().parent)
    path = Path(home) / "delegations.json"
    try:
        from hermes_cli.active_sessions import _FileLock

        with _FileLock(Path(home) / "delegations.lock"):
            merged = _read_persisted_records(path)
            for record in _records.values():
                if record.get("_home") != home:
                    continue
                owner_pid = record.get("owner_pid")
                if owner_pid != os.getpid() and not (
                    record.get("status") == "interrupted"
                    and not _pid_alive(
                        owner_pid, record.get("owner_process_start_time")
                    )
                ):
                    continue
                merged[str(record["delegation_id"])] = {
                    key: value
                    for key, value in record.items()
                    if key not in {"interrupt_fn", "_home"}
                }
            for delegation_id in remove_ids or set():
                merged.pop(delegation_id, None)
            terminal = [
                (delegation_id, record)
                for delegation_id, record in merged.items()
                if record.get("status") != "running"
            ]
            terminal.sort(
                key=lambda item: item[1].get("completed_at")
                or item[1].get("dispatched_at")
                or 0
            )
            for delegation_id, _ in terminal[:-_MAX_RETAINED_COMPLETED]:
                merged.pop(delegation_id, None)
            atomic_json_write(
                path,
                {"records": list(merged.values())},
                mode=0o600,
                default=str,
            )
    except Exception as exc:
        logger.error("Could not persist async delegation records: %s", exc)
        return False
    return True


def _reserve_record_locked(record: Dict[str, Any], max_running: int) -> str:
    """Atomically recheck profile capacity and persist one running record."""
    from hermes_cli.active_sessions import _FileLock
    from utils import atomic_json_write

    home = str(record["_home"])
    path = Path(home) / "delegations.json"
    try:
        with _FileLock(Path(home) / "delegations.lock"):
            merged = _read_persisted_records(path)
            live_running = 0
            now = time.time()
            for persisted in merged.values():
                if persisted.get("status") != "running":
                    continue
                if _pid_alive(
                    persisted.get("owner_pid"),
                    persisted.get("owner_process_start_time"),
                ):
                    live_running += 1
                else:
                    persisted["status"] = "interrupted"
                    persisted["completed_at"] = now
                    persisted["error"] = (
                        "Hermes restarted while this delegation was running. "
                        "Its saved goal and context remain available for redispatch."
                    )
            if live_running >= max_running:
                return "capacity"
            merged[str(record["delegation_id"])] = {
                key: value
                for key, value in record.items()
                if key not in {"interrupt_fn", "_home"}
            }
            atomic_json_write(
                path,
                {"records": list(merged.values())},
                mode=0o600,
                default=str,
            )
    except Exception as exc:
        logger.error("Could not reserve async delegation capacity: %s", exc)
        return "error"
    return "ok"


def _load_records_locked() -> None:
    """Refresh one profile without evicting this process's live workers."""
    path = _records_path()
    home = str(path.parent)
    persisted = _read_persisted_records(path)
    recovered = False
    now = time.time()
    for delegation_id, raw_record in persisted.items():
        current = _records.get(delegation_id)
        if (
            current is not None
            and current.get("_home") == home
            and current.get("owner_pid") == os.getpid()
        ):
            continue
        record = dict(raw_record)
        record["interrupt_fn"] = None
        record["_home"] = home
        if record.get("status") == "running" and not _pid_alive(
            record.get("owner_pid"), record.get("owner_process_start_time")
        ):
            record["status"] = "interrupted"
            record["completed_at"] = now
            record["error"] = (
                "Hermes restarted while this delegation was running. "
                "Its saved goal and context remain available for redispatch."
            )
            recovered = True
        _records[delegation_id] = record
    persisted_ids = set(persisted)
    for delegation_id, record in list(_records.items()):
        if (
            record.get("_home") == home
            and record.get("owner_pid") != os.getpid()
            and delegation_id not in persisted_ids
        ):
            _records.pop(delegation_id, None)
    before_prune = sum(1 for record in _records.values() if record.get("_home") == home)
    _prune_completed_locked(home)
    after_prune = sum(1 for record in _records.values() if record.get("_home") == home)
    if recovered or after_prune != before_prune:
        _persist_records_locked(home)


def _get_executor(max_workers: int) -> ThreadPoolExecutor:
    """Lazily create (or grow) the shared daemon executor.

    We never shrink — ThreadPoolExecutor can't resize — but if the configured
    cap grows between calls we rebuild a larger pool. Existing in-flight
    futures keep running on the old pool until it's garbage collected.
    """
    global _executor, _executor_max_workers
    with _executor_lock:
        if _executor is None or max_workers > _executor_max_workers:
            # Daemon threads: thread_name_prefix aids debugging in stack dumps.
            _executor = _DaemonThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="async-delegate",
            )
            _executor_max_workers = max_workers
        return _executor


def active_count() -> int:
    """Number of async delegations currently running for the active profile."""
    with _records_lock:
        _load_records_locked()
        home = str(_records_path().parent)
        return sum(
            1 for record in _records.values()
            if record.get("_home") == home
            and record.get("status") in {"running", "finalizing"}
        )


def _new_delegation_id() -> str:
    return f"deleg_{uuid.uuid4().hex[:8]}"


def _prune_completed_locked(home: Optional[str] = None) -> None:
    """Drop the oldest completed records beyond the per-profile retention cap.

    Caller must hold ``_records_lock``.
    """
    home = home or str(_records_path().parent)
    completed = [
        (rid, record)
        for rid, record in _records.items()
        if record.get("_home") == home and record.get("status") != "running"
    ]
    if len(completed) <= _MAX_RETAINED_COMPLETED:
        return
    # Oldest-first by completion time (fall back to dispatch time).
    completed.sort(key=lambda kv: kv[1].get("completed_at") or kv[1].get("dispatched_at") or 0)
    for rid, _ in completed[: len(completed) - _MAX_RETAINED_COMPLETED]:
        _records.pop(rid, None)


def dispatch_async_delegation(
    *,
    goal: str,
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    parent_session_id: Optional[str] = None,
    runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
) -> Dict[str, Any]:
    """Spawn ``runner`` on the daemon executor and return a handle immediately.

    Parameters
    ----------
    goal, context, toolsets, role, model
        The dispatch-time task spec, captured verbatim for the rich
        completion block.
    session_key
        The gateway session_key (from ``tools.approval.get_current_session_key``)
        captured on the parent thread BEFORE dispatch, because the daemon
        worker thread won't carry the contextvar. Used to route the
        completion back to the originating session.
    parent_session_id
        The durable ``state.db`` session id of the parent agent that spawned
        the delegation. Carried on the completion event so the gateway can
        pin routing to the spawning session instead of recovering the latest
        ``ended_at IS NULL`` row for the peer tuple (#57498).
    runner
        Zero-arg callable that builds + runs the child and returns the same
        result dict ``_run_single_child`` produces. Runs on the worker thread.
    interrupt_fn
        Optional callable to signal the child to stop (used on shutdown /
        explicit cancel).
    max_async_children
        Concurrency cap. When at capacity the dispatch is REJECTED (the caller
        should fall back to sync or tell the user) rather than queued, so a
        runaway model can't pile up unbounded background work.

    Returns
    -------
    dict
        ``{"status": "dispatched", "delegation_id": ...}`` on success, or
        ``{"status": "rejected", "error": ...}`` when at capacity.
    """
    delegation_id = _new_delegation_id()
    dispatched_at = time.time()
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "_home": str(_records_path().parent),
        "owner_pid": os.getpid(),
        "owner_process_start_time": _current_process_start_time(),
        "goal": goal,
        "context": context,
        "toolsets": list(toolsets) if toolsets else None,
        "role": role,
        "model": model,
        "session_key": session_key,
        "origin_ui_session_id": origin_ui_session_id,
        "parent_session_id": parent_session_id,
        "status": "running",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
    }
    # Capacity check and record insert under ONE lock hold — checking
    # active_count() separately would let two concurrent dispatches (e.g.
    # from different gateway sessions) both pass the check and exceed the cap.
    with _records_lock:
        _load_records_locked()
        running = sum(
            1 for r in _records.values()
            if r.get("_home") == record["_home"] and r.get("status") == "running"
        )
        if running >= max_async_children:
            return {
                "status": "rejected",
                "reason": "capacity",
                "error": (
                    f"Async delegation capacity reached ({max_async_children} "
                    f"running). Wait for one to finish (its result will re-enter "
                    f"the chat), or run this task synchronously "
                    f"(background=false). Raise delegation.max_concurrent_children in "
                    f"config.yaml to allow more concurrent background subagents."
                ),
            }
        _records[delegation_id] = record
        reservation = _reserve_record_locked(record, max_async_children)
        if reservation != "ok":
            _records.pop(delegation_id, None)
            if reservation == "capacity":
                return {
                    "status": "rejected",
                    "reason": "capacity",
                    "error": (
                        f"Async delegation capacity reached ({max_async_children} "
                        "running across this profile). Wait for one to finish."
                    ),
                }
            return {
                "status": "rejected",
                "reason": "persistence",
                "error": "Could not persist the background delegation record.",
            }

    _persist_dispatch(record)
    executor = _get_executor(max_async_children)

    def _worker() -> None:
        result: Dict[str, Any] = {}
        status = "error"
        try:
            result = runner() or {}
            status = result.get("status") or "completed"
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.exception("Async delegation %s crashed", delegation_id)
            result = {
                "status": "error",
                "summary": None,
                "error": f"{type(exc).__name__}: {exc}",
                "api_calls": 0,
                "duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        finally:
            _finalize(delegation_id, result, status)

    try:
        # Propagate the dispatching profile so the detached child resolves
        # get_hermes_home() under the right profile.
        executor.submit(propagate_context_to_thread(_worker))
    except Exception as exc:  # pragma: no cover — pool submit failure is rare
        with _records_lock:
            _records.pop(delegation_id, None)
            _persist_records_locked(
                record.get("_home"), remove_ids={delegation_id}
            )
        _delete_durable_delegation(delegation_id)
        return {
            "status": "rejected",
            "reason": "schedule",
            "error": f"Failed to schedule async delegation: {exc}",
        }

    logger.info(
        "Dispatched async delegation %s (session_key=%s): %s",
        delegation_id, session_key or "<cli>", (goal or "")[:80],
    )
    return {"status": "dispatched", "delegation_id": delegation_id}


def _push_completion_event(evt: Dict[str, Any]) -> None:
    # A worker can finish after the test/runtime reset cleared its record;
    # don't let that stale completion leak into a later consumer.
    with _records_lock:
        if evt.get("delegation_id") not in _records:
            return
    try:
        from tools.process_registry import process_registry

        process_registry.completion_queue.put(evt)
    except Exception as exc:
        logger.error(
            "Async delegation %s finished but completion enqueue failed: %s",
            evt.get("delegation_id"),
            exc,
        )


def _finalize(delegation_id: str, result: Dict[str, Any], status: str) -> None:
    """Mark a record complete and push the completion event onto the queue."""
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            return
        # Stay active until durable persistence and queue publication finish;
        # otherwise process shutdown can kill this daemon worker in the narrow
        # gap after status flips but before SQLite is committed.
        record["status"] = "finalizing"
        record["completed_at"] = time.time()
        record["interrupt_fn"] = None  # drop the closure; child is done
        event_record = dict(record)
        _prune_completed_locked(record.get("_home"))
        _persist_records_locked(record.get("_home"))

    _emit_completion_event(event_record, result, status)
    with _records_lock:
        record = _records.get(delegation_id)
        if record is not None:
            record["status"] = status
        _prune_completed_locked()


def _emit_completion_event(
    record: Dict[str, Any], result: Dict[str, Any], status: str
) -> None:
    """Push a type='async_delegation' event onto the shared completion queue.

    Best-effort: a failure here must not crash the worker, but it WOULD mean a
    silently-lost result, so we log loudly.
    """
    summary = result.get("summary")
    error = result.get("error")
    dispatched_at = record.get("dispatched_at") or time.time()
    completed_at = record.get("completed_at") or time.time()

    evt = {
        "type": "async_delegation",
        "delegation_id": record.get("delegation_id"),
        # session_key routes the completion back to the originating gateway
        # session; empty string => CLI (single-session) path.
        "session_key": record.get("session_key", ""),
        "origin_ui_session_id": record.get("origin_ui_session_id", ""),
        "parent_session_id": record.get("parent_session_id"),
        "goal": record.get("goal", ""),
        "context": record.get("context"),
        "toolsets": record.get("toolsets"),
        "role": record.get("role"),
        "model": result.get("model") or record.get("model"),
        "status": status,
        "summary": summary,
        "error": error,
        "api_calls": result.get("api_calls", 0),
        "duration_seconds": result.get(
            "duration_seconds", round(completed_at - dispatched_at, 2)
        ),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
        "exit_reason": result.get("exit_reason"),
    }
    _persist_completion(evt, result)
    _push_completion_event(evt)


def dispatch_async_delegation_batch(
    *,
    goals: List[str],
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    parent_session_id: Optional[str] = None,
    child_session_ids: Optional[List[str]] = None,
    runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
    delegation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Dispatch a WHOLE fan-out batch as ONE background unit.

    Unlike ``dispatch_async_delegation`` (which backs a single subagent),
    ``runner`` here runs the entire batch — it builds and joins on every child
    in parallel and returns the combined ``{"results": [...],
    "total_duration_seconds": N}`` dict that the synchronous path would have
    returned. We occupy ONE async slot for the whole batch (the in-batch
    parallelism is bounded separately by ``max_concurrent_children``), so a
    single ``delegate_task`` fan-out never exhausts the async pool by itself.

    When the batch finishes, a SINGLE completion event is pushed onto the
    shared ``process_registry.completion_queue`` carrying the full per-task
    ``results`` list, so the consolidated summaries re-enter the conversation
    as one message once every child is done — the chat is never blocked while
    they run.

    Returns ``{"status": "dispatched", "delegation_id": ...}`` on success or
    ``{"status": "rejected", "error": ...}`` when the async pool is at
    capacity.
    """
    delegation_id = delegation_id or _new_delegation_id()
    dispatched_at = time.time()
    n = len(goals)
    # A combined goal label for status listings / the completion header.
    combined_goal = (
        goals[0] if n == 1 else f"{n} parallel subagents: " + "; ".join(g[:40] for g in goals)
    )
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "_home": str(_records_path().parent),
        "owner_pid": os.getpid(),
        "owner_process_start_time": _current_process_start_time(),
        "goal": combined_goal,
        "goals": list(goals),
        "context": context,
        "toolsets": list(toolsets) if toolsets else None,
        "role": role,
        "model": model,
        "session_key": session_key,
        "origin_ui_session_id": origin_ui_session_id,
        "parent_session_id": parent_session_id,
        "child_session_ids": [
            str(session_id) for session_id in (child_session_ids or []) if session_id
        ],
        "status": "running",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
        "is_batch": True,
    }
    with _records_lock:
        _load_records_locked()
        running = sum(
            1 for r in _records.values()
            if r.get("_home") == record["_home"] and r.get("status") == "running"
        )
        if running >= max_async_children:
            return {
                "status": "rejected",
                "reason": "capacity",
                "error": (
                    f"Async delegation capacity reached ({max_async_children} "
                    f"running). Wait for one to finish (its result will re-enter "
                    f"the chat), or raise delegation.max_concurrent_children in "
                    f"config.yaml to allow more concurrent background units."
                ),
            }
        _records[delegation_id] = record
        reservation = _reserve_record_locked(record, max_async_children)
        if reservation != "ok":
            _records.pop(delegation_id, None)
            if reservation == "capacity":
                return {
                    "status": "rejected",
                    "reason": "capacity",
                    "error": (
                        f"Async delegation capacity reached ({max_async_children} "
                        "running across this profile). Wait for one to finish."
                    ),
                }
            return {
                "status": "rejected",
                "reason": "persistence",
                "error": "Could not persist the background delegation record.",
            }

    _persist_dispatch(record)
    executor = _get_executor(max_async_children)

    def _worker() -> None:
        combined: Dict[str, Any] = {}
        status = "error"
        try:
            combined = runner() or {}
            # Batch status: completed unless every child errored/was interrupted.
            child_results = combined.get("results") or []
            if child_results and all(
                (r.get("status") not in ("completed", "success"))
                for r in child_results
            ):
                status = "error"
            else:
                status = "completed"
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.exception("Async delegation batch %s crashed", delegation_id)
            combined = {
                "results": [],
                "error": f"{type(exc).__name__}: {exc}",
                "total_duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        finally:
            _finalize_batch(delegation_id, combined, status)

    try:
        # Propagate the dispatching profile to the detached batch children.
        executor.submit(propagate_context_to_thread(_worker))
    except Exception as exc:  # pragma: no cover
        with _records_lock:
            _records.pop(delegation_id, None)
            _persist_records_locked(
                record.get("_home"), remove_ids={delegation_id}
            )
        _delete_durable_delegation(delegation_id)
        return {
            "status": "rejected",
            "reason": "schedule",
            "error": f"Failed to schedule async delegation batch: {exc}",
        }

    logger.info(
        "Dispatched async delegation batch %s (%d task(s), session_key=%s)",
        delegation_id, n, session_key or "<cli>",
    )
    return {"status": "dispatched", "delegation_id": delegation_id}


def _finalize_batch(
    delegation_id: str, combined: Dict[str, Any], status: str
) -> None:
    """Mark a batch record complete and push ONE combined completion event."""
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            return
        record["status"] = "finalizing"
        record["completed_at"] = time.time()
        record["interrupt_fn"] = None
        event_record = dict(record)
        _prune_completed_locked(record.get("_home"))
        _persist_records_locked(record.get("_home"))

    dispatched_at = event_record.get("dispatched_at") or time.time()
    completed_at = event_record.get("completed_at") or time.time()
    evt = {
        "type": "async_delegation",
        "delegation_id": delegation_id,
        "session_key": event_record.get("session_key", ""),
        "origin_ui_session_id": event_record.get("origin_ui_session_id", ""),
        "parent_session_id": event_record.get("parent_session_id"),
        "child_session_ids": event_record.get("child_session_ids") or [],
        "goal": event_record.get("goal", ""),
        "goals": event_record.get("goals"),
        "context": event_record.get("context"),
        "toolsets": event_record.get("toolsets"),
        "role": event_record.get("role"),
        "model": event_record.get("model"),
        "status": status,
        "is_batch": True,
        # The full per-task results list — the formatter renders a
        # consolidated multi-task block from this.
        "results": combined.get("results") or [],
        # Per-task live transcript log paths (cache/delegation/live/...).
        # They persist after completion and double as the full-fidelity
        # operational record of each child's run.
        "live_transcripts": combined.get("live_transcripts"),
        "error": combined.get("error"),
        "total_duration_seconds": combined.get("total_duration_seconds"),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
    }
    _persist_completion(evt, combined)
    _push_completion_event(evt)
    with _records_lock:
        record = _records.get(delegation_id)
        if record is not None:
            record["status"] = status
        _prune_completed_locked()


def list_async_delegations() -> List[Dict[str, Any]]:
    """Snapshot of async delegations (running + recently completed).

    Safe to call from any thread. Excludes the non-serialisable interrupt_fn.
    """
    with _records_lock:
        _load_records_locked()
        home = str(_records_path().parent)
        return [
            {
                key: value
                for key, value in record.items()
                if key not in {"interrupt_fn", "_home"}
            }
            for record in _records.values()
            if record.get("_home") == home
        ]


def interrupt_all(reason: str = "shutdown") -> int:
    """Signal every running async delegation to stop. Returns how many.

    Used on ``/stop`` and gateway shutdown so a dangling background subagent
    can't keep burning tokens with no one listening. The child still emits a
    completion event (status='interrupted') via the normal finalize path.
    """
    count = 0
    home = str(_records_path().parent)
    with _records_lock:
        targets = [
            record
            for record in _records.values()
            if record.get("_home") == home and record.get("status") == "running"
        ]
    for r in targets:
        fn = r.get("interrupt_fn")
        if callable(fn):
            try:
                fn()
                count += 1
            except Exception as exc:
                logger.debug(
                    "interrupt_all: %s interrupt failed: %s",
                    r.get("delegation_id"), exc,
                )
    if count:
        logger.info("Interrupted %d async delegation(s) (%s)", count, reason)
    return count


def interrupt_for_session(
    session_key: str = "",
    origin_ui_session_id: str = "",
    parent_session_id: str = "",
    reason: str = "session_end",
) -> int:
    """Signal running async delegations owned by ONE session to stop.

    A delegation's lifecycle is bound to the session that spawned it: when
    that session ends, its in-flight background subagents must end with it —
    a completed orphan would otherwise sit on the shared completion queue
    with no live owner, either leaking into another chat or burning tokens
    with no one listening (#55578).

    Selectors (any matching field claims the record):
    - ``origin_ui_session_id``: the live TUI tab/window that commissioned it.
    - ``session_key``: the durable routing key captured at dispatch.
    - ``parent_session_id``: the spawning agent's durable session-db id —
      the right selector for gateway chats, whose ``session_key`` (the
      platform conversation key) SURVIVES a ``/new`` reset while the
      session id rotates.

    Returns how many were interrupted.
    """
    if not session_key and not origin_ui_session_id and not parent_session_id:
        return 0
    count = 0
    home = str(_records_path().parent)
    with _records_lock:
        targets = [
            r for r in _records.values()
            if r.get("_home") == home
            and r.get("status") == "running"
            and (
                (origin_ui_session_id and str(r.get("origin_ui_session_id") or "") == origin_ui_session_id)
                or (session_key and str(r.get("session_key") or "") == session_key)
                or (parent_session_id and str(r.get("parent_session_id") or "") == parent_session_id)
            )
        ]
    for r in targets:
        fn = r.get("interrupt_fn")
        if callable(fn):
            try:
                fn()
                count += 1
            except Exception as exc:
                logger.debug(
                    "interrupt_for_session: %s interrupt failed: %s",
                    r.get("delegation_id"), exc,
                )
    if count:
        logger.info(
            "Interrupted %d async delegation(s) for ending session (%s)",
            count, reason,
        )
    return count


def _reset_for_tests() -> None:
    """Test-only: clear all state, persistent records, and the executor."""
    global _executor, _executor_max_workers, _journal_cache
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
        _executor = None
        _executor_max_workers = 0
    with _journal_lock:
        _journal_cache = {}
        _restored_ids.clear()
    with _records_lock:
        _records.clear()
        try:
            _records_path().unlink(missing_ok=True)
        except OSError:
            pass
