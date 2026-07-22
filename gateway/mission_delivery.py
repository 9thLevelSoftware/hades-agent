"""Claim-fenced transport handoff for durable mission/workflow outbox rows."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from agent.operation_journal import OperationJournal
from gateway.config import Platform
from gateway.delivery import DeliveryTarget
from gateway.mission_outbox import (
    MissionOutboxStore,
    _authority_allows_destination,
    _canonical_destination,
    _canonical_json,
    delivery_capabilities,
    normalize_platform_token,
)
from hades_constants import get_hades_home, env_get, env_set
from hades_cli import missions_db as mdb
from hades_cli import workflows_db as wfdb
from hades_state import SessionDB

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutboxDrainReport:
    """Counters from one bounded durable-outbox drain."""

    claimed: int = 0
    delivered: int = 0
    failed: int = 0
    unknown: int = 0
    released: int = 0


def _active_profile_name() -> str | None:
    """Derive the active profile from HADES_HOME and validate its hint."""
    home = get_hades_home().expanduser().resolve(strict=False)
    derived = home.name if home.parent.name == "profiles" else "default"
    configured = env_get("HERMES_PROFILE", "").strip()
    if configured and configured != derived:
        return None
    return derived


class MissionOutboxDispatcher:
    """Claim due outbox rows and delegate platform I/O to ``DeliveryRouter``.

    The persistent store owns claim fencing. This dispatcher owns only the
    short-lived handoff from a valid claim to the existing delivery router.
    """

    def __init__(
        self,
        *,
        store: MissionOutboxStore,
        router: Any,
        journal: OperationJournal,
        owner_id: str,
        lease_seconds: int = 60,
        workflow_db_path: Path | None = None,
        clock: Callable[[], float] = time.time,
        delivery_timeout_seconds: float = 30.0,
    ) -> None:
        if not isinstance(store, MissionOutboxStore):
            raise TypeError("store must be a MissionOutboxStore")
        if not isinstance(owner_id, str) or not owner_id:
            raise ValueError("owner_id must be a non-empty string")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds < 1:
            raise ValueError("lease_seconds must be a positive integer")
        if workflow_db_path is not None and not isinstance(workflow_db_path, Path):
            raise TypeError("workflow_db_path must be a pathlib.Path or None")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if (
            not isinstance(delivery_timeout_seconds, (int, float))
            or isinstance(delivery_timeout_seconds, bool)
            or delivery_timeout_seconds <= 0
        ):
            raise ValueError("delivery_timeout_seconds must be positive")
        self.store = store
        self.router = router
        self.journal = journal
        self.owner_id = owner_id
        self.lease_seconds = lease_seconds
        self.workflow_db_path = workflow_db_path or wfdb.workflows_db_path()
        self._active_home = get_hades_home().expanduser().resolve(strict=False)
        self._active_profile = _active_profile_name()
        self._clock = clock
        self.delivery_timeout_seconds = float(delivery_timeout_seconds)
        self._mission_lookup_cache: dict[str, Any | None] = {}

    def _profile_store_owned(self) -> bool:
        """Return whether the dispatcher is bound to the active profile store.

        A dispatcher owns a store only when both state/workflow database paths
        are the exact database paths for the resolved active home; sharing a
        parent directory is not an ownership relationship.
        """
        if self._active_profile is None:
            return False
        expected_state = (self._active_home / "state.db").resolve(strict=False)
        expected_workflow = (self._active_home / "workflows.db").resolve(strict=False)
        actual_state = Path(self.store.db.db_path).expanduser().resolve(strict=False)
        actual_workflow = self.workflow_db_path.expanduser().resolve(strict=False)
        return actual_state == expected_state and actual_workflow == expected_workflow

    def _mission_for_row(self, mission_id: str, *, fresh: bool = False) -> Any | None:
        """Look up a mission, optionally bypassing the per-drain cache.

        The cache exists only to bound the eligibility pre-scan's workflow-db
        reads (see ``_owned_due_outbox_ids``) — it is a scan-time
        optimization, not a source of truth. Any check gating an actual
        router call must pass ``fresh=True`` so a mission revoked/terminated
        after this dispatcher's last read of it is never missed. A fresh
        read still refreshes the cache entry so later same-mission lookups
        in this drain see the newest known snapshot rather than the oldest.
        """
        if not fresh and mission_id in self._mission_lookup_cache:
            return self._mission_lookup_cache[mission_id]
        try:
            with wfdb.connect(self.workflow_db_path) as conn:
                mission = mdb.get_mission(conn, mission_id)
        except Exception:  # noqa: BLE001
            mission = None
        self._mission_lookup_cache[mission_id] = mission
        return mission

    def _owned_due_outbox_ids(self, *, now: int, limit: int) -> set[str]:
        """Return up to ``limit`` eligible outbox ids from the due queue.

        A single invalid/missing/revoked-mission row is not the same as an
        uninspectable one: the pre-scan must not stop at exactly the first
        ``limit`` due rows in queue order, or a persistently-ineligible
        prefix (foreign profile, missing mission, stale approval) would
        permanently starve every valid row behind it — most visibly at
        ``limit=1``, where a single bad row at the front blocks the whole
        batch forever (P1-3). Inspecting up to
        ``limit * _OUTBOX_CLAIM_INSPECTION_MULTIPLIER`` rows mirrors the
        same bounded "look past bad rows" allowance ``claim_due_outbox``
        already uses for its own scan, so this stays a bounded constant
        multiple of ``limit`` rather than unbounded queue-size work. The
        loop still exits as soon as ``limit`` eligible ids are found, so a
        normal (all-valid-prefix) batch costs exactly what it did before.
        """
        if not self._profile_store_owned():
            return set()
        inspection_limit = min(
            limit * self.store.db._OUTBOX_CLAIM_INSPECTION_MULTIPLIER,
            9_223_372_036_854_775_807,
        )
        rows = self.store.db._execute_read(
            lambda conn: conn.execute(
                """SELECT * FROM mission_outbox
                     WHERE ((status IN ('pending', 'scheduled') AND not_before <= ?)
                        OR (status = 'claimed' AND
                            COALESCE(lease_expires_at, updated_at + ?) <= ?))
                     ORDER BY not_before, created_at, outbox_id
                     LIMIT ?""",
                (now, self.lease_seconds, now, inspection_limit),
            ).fetchall()
        )
        owned: set[str] = set()
        for raw_row in rows:
            if len(owned) >= limit:
                break
            try:
                row = self.store.db._outbox_from_row(raw_row)
            except ValueError:
                # Leave malformed ordinary payload handling to SessionDB.claim(),
                # which atomically quarantines it. A malformed mission row has
                # no trustworthy approval or mission identity and must not be
                # claimable through this gateway boundary.
                outbox_id = raw_row["outbox_id"]
                mission_id = raw_row["mission_id"]
                if not isinstance(outbox_id, str) or not outbox_id:
                    continue
                if mission_id is None:
                    owned.add(outbox_id)
                    continue
                if not isinstance(mission_id, str):
                    continue
                # Mission lookup failures are safe blocks, not temporary claims.
                continue
            if row.mission_id is None:
                owned.add(row.outbox_id)
                continue
            mission = self._mission_for_row(row.mission_id)
            if mission is None or mission.profile != self._active_profile:
                continue
            if self._has_current_mission_approval(row, now=now, mission=mission):
                owned.add(row.outbox_id)
        return owned

    @staticmethod
    def _content_text(content: Any) -> str:
        return content if isinstance(content, str) else _canonical_json(content)

    def _has_current_mission_approval(
        self,
        row: SessionDB.OutboxRecord,
        *,
        now: int,
        mission: Any | None = None,
        fresh: bool = False,
    ) -> bool:
        if row.mission_id is None:
            return True
        if not self._profile_store_owned():
            return False
        approval = row.approval
        if not isinstance(approval, dict):
            return False
        expires_at = approval.get("expires_at")
        authority_version = approval.get("authority_version")
        if (
            not isinstance(expires_at, int)
            or isinstance(expires_at, bool)
            or expires_at <= now
            or not isinstance(authority_version, int)
            or isinstance(authority_version, bool)
            or authority_version < 1
            or approval.get("outbox_id") != row.outbox_id
            or approval.get("revision") != row.revision
            or approval.get("content_hash") != row.content_hash
            or approval.get("destination")
            != _canonical_destination(row.platform, row.target)
        ):
            return False
        if mission is None:
            mission = self._mission_for_row(row.mission_id, fresh=fresh)
        if mission is None:
            return False
        if mission.profile != self._active_profile:
            return False
        authority = mission.authority
        if (
            mission.status != "running"
            or mission.verdict is not None
            or mission.authority_version != authority_version
            or authority.get("revoked", False) is not False
            or authority.get("valid", True) is not True
        ):
            return False
        current_expires_at = authority.get("expires_at")
        if (
            not isinstance(current_expires_at, int)
            or isinstance(current_expires_at, bool)
            or current_expires_at <= now
        ):
            return False
        allowed_effects = authority.get("allowed_effects")
        if not isinstance(allowed_effects, list) or "delayed_message" not in allowed_effects:
            return False
        return _authority_allows_destination(
            authority, platform=row.platform, target=row.target
        )

    def _begin_mission_effect(self, row: SessionDB.OutboxRecord) -> bool:
        """Advance a mission delivery transaction to the router-call boundary."""
        if row.mission_id is None:
            return True
        if not row.transaction_id:
            return False
        # Fresh graph validation (P0-2) before the first phase transition: a
        # corrupted or mismatched prepared_json/destination/operation
        # identity — or a missing effect row entirely (a split ledger) —
        # must block the transition rather than being carried through
        # toward a router call.
        if not self.store.db.mission_outbox_graph_matches(row.outbox_id):
            return False
        transaction = self.store.db.get_effect_transaction(row.transaction_id)
        if transaction is None:
            return False
        phase = transaction.phase
        if phase == "pending":
            if not self.store.db.transition_effect_transaction(
                row.transaction_id,
                expected_phase="pending",
                next_phase="previewed",
                authority=row.approval,
            ):
                return False
            phase = "previewed"
        if phase != "previewed":
            return False
        return self.store.db.transition_effect_transaction(
            row.transaction_id,
            expected_phase="previewed",
            next_phase="committing",
        )

    def _adapter_for_row(self, row: SessionDB.OutboxRecord) -> Any | None:
        platform = normalize_platform_token(row.platform)
        if platform is None:
            return None
        try:
            return getattr(self.router, "adapters", {}).get(Platform(platform))
        except (TypeError, ValueError):
            return None

    def _settle_mission_effect(
        self,
        row: SessionDB.OutboxRecord,
        *,
        next_phase: str,
        expected_phase: str = "committing",
    ) -> bool:
        """Advance the linked effect to a terminal/committed phase.

        A missing effect row (deleted, or never created — a split ledger)
        has nothing left to settle at the effect layer; treat it as
        already-settled rather than raising and crashing the rest of this
        drain batch (P0-2). The outbox side of the terminal write already
        recorded whatever conservative outcome the caller chose.
        """
        if row.mission_id is None or not row.transaction_id:
            return True
        if self.store.db.get_effect_transaction(row.transaction_id) is None:
            return True
        return self.store.db.transition_effect_transaction(
            row.transaction_id,
            expected_phase=expected_phase,
            next_phase=next_phase,
        )

    def _terminalize(
        self,
        row: SessionDB.OutboxRecord,
        *,
        next_phase: str,
        write_outbox: Callable[[], bool],
        settle_pre_dispatch_effect: bool = False,
        pre_dispatch_write_outbox: Callable[[str], bool] | None = None,
    ) -> bool:
        """Atomically persist a fenced outbox terminal state and mission effect."""

        def _write(_conn: Any) -> bool:
            effect_next_phase = next_phase
            expected_effect_phase = "committing"
            terminal_effect_already_settled = False
            terminal_write = write_outbox
            if settle_pre_dispatch_effect and row.transaction_id:
                transaction = self.store.db.get_effect_transaction(row.transaction_id)
                if transaction is not None and transaction.phase in {
                    "pending",
                    "previewed",
                }:
                    effect_next_phase = "failed"
                    expected_effect_phase = transaction.phase
                    if pre_dispatch_write_outbox is not None:
                        failure_writer = pre_dispatch_write_outbox
                        terminal_write = lambda: failure_writer("failed")
                elif transaction is not None and transaction.phase == "failed":
                    effect_next_phase = None
                    terminal_effect_already_settled = True
                    if pre_dispatch_write_outbox is not None:
                        failure_writer = pre_dispatch_write_outbox
                        terminal_write = lambda: failure_writer("failed")
                elif transaction is not None and transaction.phase == "unknown_effect":
                    effect_next_phase = None
                    terminal_effect_already_settled = True
            if not terminal_write():
                return False
            if terminal_effect_already_settled:
                return True
            assert effect_next_phase is not None
            if not self._settle_mission_effect(
                row,
                next_phase=effect_next_phase,
                expected_phase=expected_effect_phase,
            ):
                raise RuntimeError("effect settlement CAS lost")
            return True

        return self.store.db._run_in_write_transaction(_write)

    def _settle_unconfirmed(
        self,
        row: SessionDB.OutboxRecord,
        *,
        error: str,
    ) -> str:
        """Persist the only safe terminal outcome after router non-success."""
        operation = self.journal.get(row.delivery_id)
        if operation is not None and operation.state == "failed":
            if self._terminalize(
                row,
                next_phase="failed",
                write_outbox=lambda: self.store.mark_failed(
                    row.outbox_id,
                    owner_id=self.owner_id,
                    claim_token=row.claim_token,
                    error=error,
                ),
            ):
                return "failed"
            return "released"
        if self._terminalize(
            row,
            next_phase="unknown_effect",
            write_outbox=lambda: self.store.mark_unknown(
                row.outbox_id,
                owner_id=self.owner_id,
                claim_token=row.claim_token,
                result={
                    "error": error,
                    "recovery_capabilities": delivery_capabilities(
                        self._adapter_for_row(row)
                    ),
                },
            ),
        ):
            return "unknown"
        return "released"

    def _reject_before_dispatch(
        self,
        row: SessionDB.OutboxRecord,
        *,
        error: str,
    ) -> str:
        """Abort a committing mission effect whose fresh pre-router check
        failed — stale authority (P0-1) or a graph mismatch (P0-2).

        The router is never invoked on this path, so — unlike
        ``_settle_unconfirmed`` — there is no delivery ambiguity to
        reconcile: no journal entry can exist for a call that never
        happened. ``unknown_effect`` would misrepresent this as needing
        reconciliation, and the effect-phase graph only permits
        ``committing -> {committed, unknown_effect, failed}`` (no path back
        to ``previewed``/``pending``), so ``failed`` is the only outcome
        that is both accurate and legal here, regardless of which check
        rejected it. A fresh materialize + ``requeue_terminal`` with a
        renewed approval can retry it later.
        """
        if self._terminalize(
            row,
            next_phase="failed",
            write_outbox=lambda: self.store.mark_failed(
                row.outbox_id,
                owner_id=self.owner_id,
                claim_token=row.claim_token,
                error=error,
            ),
        ):
            return "failed"
        return "released"

    @staticmethod
    def _detach_delivery_task(task: "asyncio.Task[Any]") -> None:
        """Let a cancelled-but-still-running delivery task finish unobserved.

        Never awaited further by the drain loop — doing so would reintroduce
        the exact unbounded wait the deadline in :meth:`_deliver_with_deadline`
        exists to prevent. This done-callback only silences Python's
        "exception was never retrieved" warning for the detached task; it
        does not change when, or whether, the task actually stops running.
        """

        def _drain_result(finished: "asyncio.Task[Any]") -> None:
            if finished.cancelled():
                return
            exc = finished.exception()
            if exc is not None:
                logger.warning(
                    "detached router delivery task ended with %s: %s",
                    type(exc).__name__,
                    exc,
                )

        task.add_done_callback(_drain_result)

    async def _deliver_with_deadline(
        self,
        row: SessionDB.OutboxRecord,
        target: DeliveryTarget,
    ) -> Any:
        """Await the router's delivery call with a deadline the router
        itself cannot bypass by ignoring cancellation (P1-4).

        ``asyncio.wait_for`` cancels its wrapped awaitable on timeout but
        then still awaits it to completion with no further bound — a
        router coroutine that catches/suppresses ``CancelledError`` (or
        shields itself) can hold that await open indefinitely, silently
        turning the configured ``delivery_timeout_seconds`` into no
        deadline at all. ``asyncio.wait`` with a timeout never blocks past
        it regardless of whether the task actually finishes, so a
        cancellation-resistant router can only ever delay this dispatcher
        by the configured deadline — never longer. A still-pending task is
        cancelled and detached (see :meth:`_detach_delivery_task`); this
        coroutine never waits for that cancellation to actually land.

        Raises ``asyncio.TimeoutError`` on a missed deadline and
        ``asyncio.CancelledError`` if this coroutine's own caller is
        cancelled — both handled by the caller's existing exception
        handling exactly like a normal ``asyncio.wait_for`` timeout would
        be, settling the row as unknown/ambiguous rather than delivered.
        """
        task = asyncio.ensure_future(
            self.router.deliver(
                self._content_text(row.content),
                [target],
                metadata={"delivery_id": row.delivery_id},
            )
        )
        try:
            done, pending = await asyncio.wait(
                {task}, timeout=self.delivery_timeout_seconds
            )
        except asyncio.CancelledError:
            task.cancel()
            self._detach_delivery_task(task)
            raise
        if task in pending:
            task.cancel()
            self._detach_delivery_task(task)
            raise asyncio.TimeoutError(
                f"router delivery exceeded {self.delivery_timeout_seconds}s "
                "deadline and did not honor cancellation"
            )
        return task.result()

    async def drain(
        self,
        *,
        now: Optional[int] = None,
        limit: int = 50,
    ) -> OutboxDrainReport:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        self._mission_lookup_cache.clear()
        claim_now = int(self._clock()) if now is None else now
        eligible_outbox_ids = self._owned_due_outbox_ids(now=claim_now, limit=limit)
        claimed_rows = self.store.claim(
            now=claim_now,
            owner_id=self.owner_id,
            lease_seconds=self.lease_seconds,
            limit=limit,
            eligible_outbox_ids=eligible_outbox_ids,
            require_mission_approval=True,
        )
        delivered = 0
        failed = 0
        unknown = 0
        released = 0
        for row in claimed_rows:
            dispatch_now = claim_now if now is not None else int(self._clock())
            if not row.claim_token:
                released += 1
                continue
            if not self._has_current_mission_approval(row, now=dispatch_now):
                if self.store.release(
                    row.outbox_id,
                    owner_id=self.owner_id,
                    claim_token=row.claim_token,
                ):
                    released += 1
                continue
            adapter = self._adapter_for_row(row)
            if not self.store.set_delivery_capabilities(row.outbox_id, adapter=adapter):
                effect_dispatchable = False
            else:
                effect_dispatchable = self._begin_mission_effect(row)
            if not effect_dispatchable:
                if self._terminalize(
                    row,
                    next_phase="unknown_effect",
                    write_outbox=lambda: self.store.mark_unknown(
                        row.outbox_id,
                        owner_id=self.owner_id,
                        claim_token=row.claim_token,
                        result={"error": "mission effect transaction is not dispatchable"},
                    ),
                    settle_pre_dispatch_effect=True,
                    pre_dispatch_write_outbox=lambda phase: (
                        self.store.mark_failed(
                            row.outbox_id,
                            owner_id=self.owner_id,
                            claim_token=row.claim_token,
                            result={"error": "mission effect transaction is not dispatchable"},
                        )
                        if phase == "failed"
                        else self.store.mark_unknown(
                            row.outbox_id,
                            owner_id=self.owner_id,
                            claim_token=row.claim_token,
                            result={"error": "mission effect transaction is not dispatchable"},
                        )
                    ),
                ):
                    terminal = self.store.get_by_id(row.outbox_id)
                    if terminal is not None and terminal.status == "failed":
                        failed += 1
                    else:
                        unknown += 1
                else:
                    released += 1
                continue
            router_now = claim_now if now is not None else int(self._clock())
            # Fresh, not cached: this is the last gate before an adapter call,
            # so it must observe a revoke/version-bump/terminalization/
            # profile-change that happened after the earlier checks above —
            # not the mission snapshot this dispatcher happened to read
            # earlier in the same drain.
            if not self._has_current_mission_approval(
                row, now=router_now, fresh=True
            ):
                settlement = self._reject_before_dispatch(
                    row,
                    error="mission authority is no longer current before router delivery",
                )
                if settlement == "failed":
                    failed += 1
                else:
                    released += 1
                continue
            # Fresh graph validation (P0-2), same rationale as the fresh
            # authority check above: a corrupted or mismatched
            # prepared_json/destination/operation identity — or the effect
            # row vanishing outright — that appears after _begin_mission_effect
            # already advanced this row to "committing" must still block the
            # router call.
            if not self.store.db.mission_outbox_graph_matches(row.outbox_id):
                settlement = self._reject_before_dispatch(
                    row,
                    error="mission outbox/operation/effect graph is inconsistent before router delivery",
                )
                if settlement == "failed":
                    failed += 1
                else:
                    released += 1
                continue
            try:
                normalized_platform = normalize_platform_token(row.platform)
                if normalized_platform is None:
                    raise ValueError("outbox platform is not a safe token")
                target = DeliveryTarget(
                    platform=Platform(normalized_platform),
                    chat_id=row.target,
                )
                results = await self._deliver_with_deadline(row, target)
            except asyncio.CancelledError as exc:
                self._settle_unconfirmed(
                    row, error=f"{type(exc).__name__}: router delivery cancelled"
                )
                raise
            except Exception as exc:
                settlement = self._settle_unconfirmed(
                    row, error=f"{type(exc).__name__}: {exc}"
                )
                if settlement == "failed":
                    failed += 1
                elif settlement == "unknown":
                    unknown += 1
                else:
                    released += 1
                continue
            outcome = results.get(target.to_string()) if isinstance(results, dict) else None
            if not isinstance(outcome, dict) or outcome.get("success") is not True:
                error = (
                    str(outcome.get("error") or "router reported delivery failure")
                    if isinstance(outcome, dict)
                    else "router returned no target outcome"
                )
                settlement = self._settle_unconfirmed(row, error=error)
                if settlement == "failed":
                    failed += 1
                elif settlement == "unknown":
                    unknown += 1
                else:
                    released += 1
                continue
            if row.platform != Platform.LOCAL.value:
                operation = self.journal.get(row.delivery_id)
                if operation is None or operation.state != "confirmed":
                    settlement = self._settle_unconfirmed(
                        row,
                        error="router returned without a confirmed delivery journal",
                    )
                    if settlement == "failed":
                        failed += 1
                    elif settlement == "unknown":
                        unknown += 1
                    else:
                        released += 1
                    continue
            result = outcome.get("result")
            if self._terminalize(
                row,
                next_phase="committed",
                write_outbox=lambda: self.store.mark_delivered(
                    row.outbox_id,
                    owner_id=self.owner_id,
                    claim_token=row.claim_token,
                    result=result,
                ),
            ):
                delivered += 1
            else:
                released += 1
        return OutboxDrainReport(
            claimed=len(claimed_rows),
            delivered=delivered,
            failed=failed,
            unknown=unknown,
            released=released,
        )


__all__ = ["MissionOutboxDispatcher", "OutboxDrainReport"]
