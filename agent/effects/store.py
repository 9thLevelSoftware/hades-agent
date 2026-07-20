"""Typed SQLite access for action transactions over ``SessionDB``.

All writes go through ``SessionDB._execute_write`` (BEGIN IMMEDIATE, jitter
retry) and all reads through ``_execute_read``. Revisions and events are
immutable once written; state changes happen only through explicit CAS
methods that return ``False`` on a lost race instead of overwriting.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterable, Mapping, Optional

from hades_state import SessionDB

from agent.effects.models import (
    ActionTransaction,
    EffectTransaction,
    ImmutableRecordError,
    RevisionEdge,
    RevisionNode,
    TransactionEvent,
    TransactionRevision,
    TransactionSnapshot,
    canonical_json,
    graph_content_hash,
    normalize_graph_input,
    validate_failure_policy,
    validate_phase,
    validate_status,
)

__all__ = ["TransactionStore"]


def _decode(raw: Optional[str]) -> Optional[Any]:
    if raw is None:
        return None
    return json.loads(raw)


class TransactionStore:
    """Profile-local durable store for transaction aggregates."""

    def __init__(self, db: SessionDB, *, clock=None):
        self._db = db
        self._clock = clock or (lambda: int(time.time() * 1000))

    @property
    def db(self) -> SessionDB:
        return self._db

    def _now_ms(self) -> int:
        return int(self._clock())

    # ── Row decoding ─────────────────────────────────────────────────

    @staticmethod
    def _transaction_record(row) -> ActionTransaction:
        return ActionTransaction(
            transaction_id=row["transaction_id"],
            profile=row["profile"],
            title=row["title"],
            status=row["status"],
            current_revision=row["current_revision"],
            authority_version=row["authority_version"],
            authority=_decode(row["authority_json"]) or {},
            failure_policy=row["failure_policy"],
            receipt_id=row["receipt_id"],
            created_at_ms=row["created_at_ms"],
            updated_at_ms=row["updated_at_ms"],
        )

    @staticmethod
    def _effect_record(row) -> EffectTransaction:
        return EffectTransaction(
            effect_id=row["effect_id"],
            transaction_id=row["transaction_id"],
            revision=row["revision"],
            node_id=row["node_id"],
            operation_id=row["operation_id"],
            adapter_id=row["adapter_id"],
            phase=row["phase"],
            semantics=_decode(row["semantics_json"]) or {},
            prepared=_decode(row["prepared_json"]),
            preview=_decode(row["preview_json"]),
            preview_hash=row["preview_hash"],
            authority=_decode(row["authority_json"]),
            result=_decode(row["result_json"]),
            verification=_decode(row["verification_json"]),
            reconciliation=_decode(row["reconciliation_json"]),
            created_at_ms=row["created_at_ms"],
            updated_at_ms=row["updated_at_ms"],
        )

    @staticmethod
    def _event_record(row) -> TransactionEvent:
        return TransactionEvent(
            event_id=row["event_id"],
            transaction_id=row["transaction_id"],
            kind=row["kind"],
            effect_id=row["effect_id"],
            payload=_decode(row["payload_json"]) or {},
            idempotency_key=row["idempotency_key"],
            created_at_ms=row["created_at_ms"],
        )

    # ── Transactions and revisions ───────────────────────────────────

    def create_transaction(
        self,
        *,
        transaction_id: str,
        profile: str,
        title: str,
        authority: Mapping[str, Any],
        graph: Mapping[str, Any],
        failure_policy: str,
        reason: str = "initial plan",
    ) -> ActionTransaction:
        if not transaction_id:
            raise ValueError("transaction_id must be a non-empty string")
        if not isinstance(authority, Mapping):
            raise ValueError("authority must be a mapping")
        validate_failure_policy(failure_policy)
        nodes, edges = normalize_graph_input(graph)
        graph_hash = graph_content_hash(nodes, edges)
        authority_version = int(authority.get("authority_version", 1))
        authority_json = canonical_json(authority)
        now = self._now_ms()

        def _create(conn):
            conn.execute(
                """INSERT INTO action_transactions (
                       transaction_id, profile, title, status, current_revision,
                       authority_version, authority_json, failure_policy,
                       receipt_id, created_at_ms, updated_at_ms
                   ) VALUES (?, ?, ?, 'draft', 1, ?, ?, ?, NULL, ?, ?)""",
                (
                    transaction_id, profile, title,
                    authority_version, authority_json, failure_policy,
                    now, now,
                ),
            )
            self._insert_revision_rows(
                conn,
                transaction_id=transaction_id,
                revision=1,
                base_revision=None,
                reason=reason,
                graph_hash=graph_hash,
                nodes=nodes,
                edges=edges,
                created_at_ms=now,
            )
            self._insert_event_row(
                conn,
                transaction_id=transaction_id,
                kind="transaction_created",
                effect_id=None,
                payload={"revision": 1, "graph_hash": graph_hash},
                idempotency_key=f"transaction_created:{transaction_id}",
                created_at_ms=now,
            )
            return conn.execute(
                "SELECT * FROM action_transactions WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchone()

        return self._transaction_record(self._db._execute_write(_create))

    def _insert_revision_rows(
        self,
        conn,
        *,
        transaction_id: str,
        revision: int,
        base_revision: Optional[int],
        reason: str,
        graph_hash: str,
        nodes: Iterable[RevisionNode],
        edges: Iterable[RevisionEdge],
        created_at_ms: int,
    ) -> None:
        conn.execute(
            """INSERT INTO transaction_revisions (
                   transaction_id, revision, base_revision, reason,
                   graph_hash, preview_hash, created_at_ms
               ) VALUES (?, ?, ?, ?, ?, NULL, ?)""",
            (transaction_id, revision, base_revision, reason, graph_hash,
             created_at_ms),
        )
        for node in nodes:
            conn.execute(
                """INSERT INTO transaction_revision_nodes (
                       transaction_id, revision, node_id, adapter_id, action,
                       args_json, resource_keys_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    transaction_id, revision, node.node_id, node.adapter_id,
                    node.action, canonical_json(node.args),
                    canonical_json(sorted(node.resource_keys)),
                ),
            )
        for edge in edges:
            conn.execute(
                """INSERT INTO transaction_revision_edges (
                       transaction_id, revision, parent_node_id, child_node_id
                   ) VALUES (?, ?, ?, ?)""",
                (transaction_id, revision, edge.parent_node_id,
                 edge.child_node_id),
            )

    def get_transaction(self, transaction_id: str) -> Optional[ActionTransaction]:
        def _read(conn):
            return conn.execute(
                "SELECT * FROM action_transactions WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchone()

        row = self._db._execute_read(_read)
        return self._transaction_record(row) if row is not None else None

    def get_revision(
        self, transaction_id: str, revision: int
    ) -> Optional[TransactionRevision]:
        def _read(conn):
            head = conn.execute(
                """SELECT * FROM transaction_revisions
                    WHERE transaction_id = ? AND revision = ?""",
                (transaction_id, revision),
            ).fetchone()
            if head is None:
                return None
            nodes = conn.execute(
                """SELECT * FROM transaction_revision_nodes
                    WHERE transaction_id = ? AND revision = ?
                    ORDER BY node_id""",
                (transaction_id, revision),
            ).fetchall()
            edges = conn.execute(
                """SELECT * FROM transaction_revision_edges
                    WHERE transaction_id = ? AND revision = ?
                    ORDER BY parent_node_id, child_node_id""",
                (transaction_id, revision),
            ).fetchall()
            return head, nodes, edges

        result = self._db._execute_read(_read)
        if result is None:
            return None
        head, node_rows, edge_rows = result
        return TransactionRevision(
            transaction_id=head["transaction_id"],
            revision=head["revision"],
            base_revision=head["base_revision"],
            reason=head["reason"],
            graph_hash=head["graph_hash"],
            preview_hash=head["preview_hash"],
            created_at_ms=head["created_at_ms"],
            nodes=tuple(
                RevisionNode(
                    node_id=row["node_id"],
                    adapter_id=row["adapter_id"],
                    action=row["action"],
                    args=_decode(row["args_json"]) or {},
                    resource_keys=tuple(_decode(row["resource_keys_json"]) or ()),
                )
                for row in node_rows
            ),
            edges=tuple(
                RevisionEdge(
                    parent_node_id=row["parent_node_id"],
                    child_node_id=row["child_node_id"],
                )
                for row in edge_rows
            ),
        )

    def create_revision(
        self,
        *,
        transaction_id: str,
        expected_revision: int,
        nodes: tuple[RevisionNode, ...],
        edges: tuple[RevisionEdge, ...],
        reason: str,
        superseded_effect_ids: Iterable[str] = (),
        expected_phases: Optional[Mapping[str, str]] = None,
    ) -> "TransactionRevision":
        """Atomically persist revision ``expected_revision + 1``.

        Single write transaction: the current-revision CAS, revision/node/
        edge inserts, superseded-attempt transitions, and the
        ``revision_created`` event all land together or not at all. A CAS
        miss raises :class:`RevisionConflict` with no partial writes.

        ``expected_phases`` makes the caller's frozen-node validation
        atomic with the install: the latest per-node effect phases are
        re-read INSIDE this write transaction and any drift from the
        snapshot the validation saw (for example a node moving
        ``previewed → committing`` under a racing commit) raises
        :class:`RevisionConflict` instead of installing a graph that
        rewrites executing work. A transaction that is mid-commit or
        mid-compensation refuses revision outright.
        """
        from agent.effects.models import RevisionConflict, graph_content_hash

        new_revision = expected_revision + 1
        graph_hash = graph_content_hash(nodes, edges)
        superseded = tuple(superseded_effect_ids)
        now = self._now_ms()

        def _create(conn):
            row = conn.execute(
                """SELECT current_revision, status FROM action_transactions
                    WHERE transaction_id = ?""",
                (transaction_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown transaction {transaction_id!r}")
            current = row["current_revision"]
            if row["status"] in {"committing", "compensating"}:
                raise RevisionConflict(
                    f"transaction {transaction_id!r} is {row['status']}; "
                    "revise after the in-flight work settles"
                )
            if expected_phases is not None:
                # Atomic re-validation: the phases the caller's frozen-node
                # truth table saw must still hold inside THIS write
                # transaction. Frozen wins over later pending attempts,
                # mirroring latest_effects_by_node().
                frozen_phases = (
                    "committing", "committed", "verified", "compensating",
                    "compensated", "unknown_effect",
                )
                live: dict[str, str] = {}
                frozen_nodes: set[str] = set()
                for phase_row in conn.execute(
                    """SELECT node_id, phase FROM transaction_effects
                        WHERE transaction_id = ?
                        ORDER BY revision, node_id, effect_id""",
                    (transaction_id,),
                ).fetchall():
                    if phase_row["node_id"] in frozen_nodes:
                        continue
                    live[phase_row["node_id"]] = phase_row["phase"]
                    if phase_row["phase"] in frozen_phases:
                        frozen_nodes.add(phase_row["node_id"])
                drifted = sorted(
                    node_id
                    for node_id in set(live) | set(expected_phases)
                    if live.get(node_id) != expected_phases.get(node_id)
                )
                if drifted:
                    raise RevisionConflict(
                        f"effect phases changed under revision for nodes "
                        f"{drifted}; re-validate against current state"
                    )
            cursor = conn.execute(
                """UPDATE action_transactions
                       SET current_revision = ?, status = 'draft',
                           updated_at_ms = ?
                     WHERE transaction_id = ? AND current_revision = ?""",
                (new_revision, now, transaction_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict(
                    f"expected revision {expected_revision}, current {current}"
                )
            self._insert_revision_rows(
                conn,
                transaction_id=transaction_id,
                revision=new_revision,
                base_revision=expected_revision,
                reason=reason,
                graph_hash=graph_hash,
                nodes=nodes,
                edges=edges,
                created_at_ms=now,
            )
            if superseded:
                placeholders = ",".join("?" for _ in superseded)
                conn.execute(
                    f"""UPDATE transaction_effects
                           SET phase = 'superseded', updated_at_ms = ?
                         WHERE effect_id IN ({placeholders})
                           AND phase IN ('prepared', 'previewed')""",
                    (now, *superseded),
                )
            self._insert_event_row(
                conn,
                transaction_id=transaction_id,
                kind="revision_created",
                effect_id=None,
                payload={
                    "revision": new_revision,
                    "base_revision": expected_revision,
                    "reason": reason,
                    "graph_hash": graph_hash,
                    "superseded_effects": sorted(superseded),
                },
                idempotency_key=f"revision_created:{transaction_id}:{new_revision}",
                created_at_ms=now,
            )
            return True

        self._db._execute_write(_create)
        return self.get_revision(transaction_id, new_revision)

    def set_revision_preview_hash(
        self, transaction_id: str, revision: int, preview_hash: str
    ) -> bool:
        """Stamp the ordered-preview hash on a revision (once per preview)."""

        def _set(conn):
            cursor = conn.execute(
                """UPDATE transaction_revisions SET preview_hash = ?
                    WHERE transaction_id = ? AND revision = ?""",
                (preview_hash, transaction_id, revision),
            )
            return cursor.rowcount == 1

        return self._db._execute_write(_set)

    def list_transactions_by_status(
        self, statuses: set[str]
    ) -> tuple[ActionTransaction, ...]:
        for status in statuses:
            validate_status(status)
        placeholders = ",".join("?" for _ in statuses)

        def _read(conn):
            return conn.execute(
                f"""SELECT * FROM action_transactions
                     WHERE status IN ({placeholders})
                     ORDER BY created_at_ms, transaction_id""",
                tuple(statuses),
            ).fetchall()

        rows = self._db._execute_read(_read)
        return tuple(self._transaction_record(row) for row in rows)

    def get_node(
        self, transaction_id: str, revision: int, node_id: str
    ) -> Optional[RevisionNode]:
        def _read(conn):
            return conn.execute(
                """SELECT * FROM transaction_revision_nodes
                    WHERE transaction_id = ? AND revision = ? AND node_id = ?""",
                (transaction_id, revision, node_id),
            ).fetchone()

        row = self._db._execute_read(_read)
        if row is None:
            return None
        return RevisionNode(
            node_id=row["node_id"],
            adapter_id=row["adapter_id"],
            action=row["action"],
            args=_decode(row["args_json"]) or {},
            resource_keys=tuple(_decode(row["resource_keys_json"]) or ()),
        )

    def replace_revision(
        self, transaction_id: str, revision: int, graph: Mapping[str, Any]
    ) -> None:
        """Revisions are immutable snapshots; replacement is a contract
        violation, always. New plans go through ``create_revision``."""
        raise ImmutableRecordError(
            f"revision {revision} of transaction {transaction_id!r} is "
            "immutable; create a new revision instead"
        )

    # ── Effects ──────────────────────────────────────────────────────

    def create_effect_attempt(
        self,
        *,
        effect_id: str,
        transaction_id: str,
        revision: int,
        node_id: str,
        operation_id: str,
        adapter_id: str,
        semantics: Optional[Mapping[str, Any]] = None,
    ) -> EffectTransaction:
        if not effect_id or not operation_id:
            raise ValueError("effect_id and operation_id are required")
        now = self._now_ms()

        def _create(conn):
            conn.execute(
                """INSERT INTO transaction_effects (
                       effect_id, transaction_id, revision, node_id,
                       operation_id, adapter_id, phase, semantics_json,
                       prepared_json, preview_json, preview_hash,
                       authority_json, result_json, verification_json,
                       reconciliation_json, created_at_ms, updated_at_ms
                   ) VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, NULL, NULL,
                             NULL, NULL, NULL, NULL, NULL, ?, ?)""",
                (
                    effect_id, transaction_id, revision, node_id,
                    operation_id, adapter_id,
                    canonical_json(dict(semantics or {})),
                    now, now,
                ),
            )
            return conn.execute(
                "SELECT * FROM transaction_effects WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()

        return self._effect_record(self._db._execute_write(_create))

    def effect_for(
        self, transaction_id: str, revision: int, node_id: str
    ) -> Optional[EffectTransaction]:
        def _read(conn):
            return conn.execute(
                """SELECT * FROM transaction_effects
                    WHERE transaction_id = ? AND revision = ? AND node_id = ?""",
                (transaction_id, revision, node_id),
            ).fetchone()

        row = self._db._execute_read(_read)
        return self._effect_record(row) if row is not None else None

    def list_effects(self, transaction_id: str) -> tuple[EffectTransaction, ...]:
        def _read(conn):
            return conn.execute(
                """SELECT * FROM transaction_effects
                    WHERE transaction_id = ?
                    ORDER BY revision, node_id, effect_id""",
                (transaction_id,),
            ).fetchall()

        rows = self._db._execute_read(_read)
        return tuple(self._effect_record(row) for row in rows)

    def latest_effects_by_node(
        self, transaction_id: str
    ) -> dict[str, EffectTransaction]:
        """Latest attempt per node id (highest revision wins).

        Frozen effects live at the revision that committed them; every
        consumer that asks "what is the truth for this node" must look
        across revisions, never only at the current one — a committed
        node from revision 1 stays the node's truth in revision 2.
        """
        frozen_phases = {
            "committing", "committed", "verified", "compensating",
            "compensated", "unknown_effect",
        }
        latest: dict[str, EffectTransaction] = {}
        frozen: set[str] = set()
        for effect in self.list_effects(transaction_id):
            # list_effects is ordered by revision ASC, so later revisions
            # overwrite earlier ones per node — EXCEPT that a frozen
            # attempt is the node's permanent truth: a stray later
            # pending attempt can never shadow committed history.
            if effect.node_id in frozen:
                continue
            latest[effect.node_id] = effect
            if effect.phase in frozen_phases:
                frozen.add(effect.node_id)
        return latest

    def latest_effect_phases(self, transaction_id: str) -> dict[str, str]:
        """Latest attempt phase per node id (highest revision wins)."""
        return {
            node_id: effect.phase
            for node_id, effect in self.latest_effects_by_node(
                transaction_id
            ).items()
        }

    def get_effect(self, effect_id: str) -> Optional[EffectTransaction]:
        def _read(conn):
            return conn.execute(
                "SELECT * FROM transaction_effects WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()

        row = self._db._execute_read(_read)
        return self._effect_record(row) if row is not None else None

    def get_effect_by_operation_id(
        self, operation_id: str
    ) -> Optional[EffectTransaction]:
        def _read(conn):
            return conn.execute(
                "SELECT * FROM transaction_effects WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()

        row = self._db._execute_read(_read)
        return self._effect_record(row) if row is not None else None

    def transition_effect(
        self,
        effect_id: str,
        from_phases: set[str],
        to_phase: str,
        *,
        updates: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        """CAS the effect phase; returns False when the current phase is
        not in *from_phases*. Optional *updates* set JSON evidence columns
        atomically with the phase change."""
        validate_phase(to_phase)
        for phase in from_phases:
            validate_phase(phase)
        allowed_columns = {
            "semantics_json", "prepared_json", "preview_json", "preview_hash",
            "authority_json", "result_json", "verification_json",
            "reconciliation_json",
        }
        extra = dict(updates or {})
        unknown = set(extra) - allowed_columns
        if unknown:
            raise ValueError(f"unknown effect columns {sorted(unknown)}")
        placeholders = ",".join("?" for _ in from_phases)
        set_clauses = ["phase = ?", "updated_at_ms = ?"]
        set_params: list[Any] = [to_phase, self._now_ms()]
        for column, value in sorted(extra.items()):
            set_clauses.append(f"{column} = ?")
            if column == "preview_hash" or value is None:
                set_params.append(value)
            else:
                set_params.append(
                    value if isinstance(value, str) else canonical_json(value)
                )

        def _transition(conn):
            cursor = conn.execute(
                f"""UPDATE transaction_effects
                       SET {', '.join(set_clauses)}
                     WHERE effect_id = ? AND phase IN ({placeholders})""",
                (*set_params, effect_id, *from_phases),
            )
            return cursor.rowcount == 1

        return self._db._execute_write(_transition)

    # ── Transaction status CAS ───────────────────────────────────────

    def transition_status(
        self, transaction_id: str, from_statuses: set[str], to_status: str
    ) -> bool:
        validate_status(to_status)
        for status in from_statuses:
            validate_status(status)
        placeholders = ",".join("?" for _ in from_statuses)

        def _transition(conn):
            cursor = conn.execute(
                f"""UPDATE action_transactions
                       SET status = ?, updated_at_ms = ?
                     WHERE transaction_id = ? AND status IN ({placeholders})""",
                (to_status, self._now_ms(), transaction_id, *from_statuses),
            )
            return cursor.rowcount == 1

        return self._db._execute_write(_transition)

    def set_receipt_id(self, transaction_id: str, receipt_id: str) -> bool:
        """Project the issued shared-receipt id onto the aggregate row."""

        def _set(conn):
            # Deliberately does NOT touch updated_at_ms: projecting the
            # receipt id is bookkeeping, not a state change, and evidence
            # timestamps derive from updated_at_ms for deterministic
            # re-issue.
            cursor = conn.execute(
                """UPDATE action_transactions
                       SET receipt_id = ?
                     WHERE transaction_id = ?
                       AND (receipt_id IS NULL OR receipt_id = ?)""",
                (receipt_id, transaction_id, receipt_id),
            )
            return cursor.rowcount == 1

        return self._db._execute_write(_set)

    # ── Compensation attempts ────────────────────────────────────────

    @staticmethod
    def _compensation_record(row):
        from agent.effects.models import CompensationAttempt

        return CompensationAttempt(
            compensation_id=row["compensation_id"],
            effect_id=row["effect_id"],
            operation_id=row["operation_id"],
            fidelity=row["fidelity"],
            status=row["status"],
            authority=_decode(row["authority_json"]) or {},
            before=_decode(row["before_json"]),
            result=_decode(row["result_json"]),
            verification=_decode(row["verification_json"]),
            error=row["error"],
            created_at_ms=row["created_at_ms"],
            updated_at_ms=row["updated_at_ms"],
        )

    def insert_compensation(
        self,
        *,
        compensation_id: str,
        effect_id: str,
        operation_id: str,
        fidelity: str,
        authority: Mapping[str, Any],
        before: Optional[Mapping[str, Any]] = None,
    ):
        now = self._now_ms()

        def _insert(conn):
            existing = conn.execute(
                "SELECT * FROM effect_compensations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if existing is not None:
                return existing
            conn.execute(
                """INSERT INTO effect_compensations (
                       compensation_id, effect_id, operation_id, fidelity,
                       status, authority_json, before_json, result_json,
                       verification_json, error, created_at_ms, updated_at_ms
                   ) VALUES (?, ?, ?, ?, 'running', ?, ?, NULL, NULL, NULL,
                             ?, ?)""",
                (
                    compensation_id, effect_id, operation_id, fidelity,
                    canonical_json(dict(authority)),
                    None if before is None else canonical_json(dict(before)),
                    now, now,
                ),
            )
            return conn.execute(
                "SELECT * FROM effect_compensations WHERE compensation_id = ?",
                (compensation_id,),
            ).fetchone()

        return self._compensation_record(self._db._execute_write(_insert))

    def finish_compensation(
        self,
        compensation_id: str,
        *,
        status: str,
        result: Optional[Mapping[str, Any]] = None,
        verification: Optional[Mapping[str, Any]] = None,
        error: Optional[str] = None,
    ) -> bool:
        if status not in {"compensated", "blocked", "failed"}:
            raise ValueError(f"invalid compensation status {status!r}")

        def _finish(conn):
            cursor = conn.execute(
                """UPDATE effect_compensations
                       SET status = ?, result_json = ?, verification_json = ?,
                           error = ?, updated_at_ms = ?
                     WHERE compensation_id = ? AND status = 'running'""",
                (
                    status,
                    None if result is None else canonical_json(dict(result)),
                    None if verification is None
                    else canonical_json(dict(verification)),
                    error, self._now_ms(), compensation_id,
                ),
            )
            return cursor.rowcount == 1

        return self._db._execute_write(_finish)

    def get_compensation_by_operation_id(self, operation_id: str):
        def _read(conn):
            return conn.execute(
                "SELECT * FROM effect_compensations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()

        row = self._db._execute_read(_read)
        return self._compensation_record(row) if row is not None else None

    def list_compensations(self, transaction_id: str) -> tuple:
        def _read(conn):
            return conn.execute(
                """SELECT ec.* FROM effect_compensations AS ec
                     JOIN transaction_effects AS te
                       ON te.effect_id = ec.effect_id
                    WHERE te.transaction_id = ?
                    ORDER BY ec.created_at_ms, ec.compensation_id""",
                (transaction_id,),
            ).fetchall()

        rows = self._db._execute_read(_read)
        return tuple(self._compensation_record(row) for row in rows)

    # ── Approval bindings ────────────────────────────────────────────

    def insert_approval(self, binding) -> None:
        """Persist one immutable approval binding row."""

        def _insert(conn):
            conn.execute(
                """INSERT INTO transaction_approvals (
                       approval_id, transaction_id, revision, node_id,
                       operation, args_hash, preview_hash, resources_json,
                       authority_version, requester, channel, decision,
                       expires_at_ms, consumed_at_ms, created_at_ms
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    binding.approval_id, binding.transaction_id,
                    binding.revision, binding.node_id, binding.operation,
                    binding.args_hash, binding.preview_hash,
                    canonical_json(sorted(binding.resources)),
                    binding.authority_version, binding.requester,
                    binding.channel, binding.decision, binding.expires_at_ms,
                    binding.consumed_at_ms, binding.created_at_ms,
                ),
            )

        self._db._execute_write(_insert)

    def find_approvals(
        self, transaction_id: str, revision: int, node_id: str
    ) -> tuple:
        from agent.effects.authority import ApprovalBinding

        def _read(conn):
            return conn.execute(
                """SELECT * FROM transaction_approvals
                    WHERE transaction_id = ? AND revision = ? AND node_id = ?
                    ORDER BY created_at_ms, approval_id""",
                (transaction_id, revision, node_id),
            ).fetchall()

        rows = self._db._execute_read(_read)
        return tuple(
            ApprovalBinding(
                approval_id=row["approval_id"],
                transaction_id=row["transaction_id"],
                revision=row["revision"],
                node_id=row["node_id"],
                operation=row["operation"],
                args_hash=row["args_hash"],
                preview_hash=row["preview_hash"],
                resources=tuple(_decode(row["resources_json"]) or ()),
                authority_version=row["authority_version"],
                requester=row["requester"],
                channel=row["channel"],
                decision=row["decision"],
                expires_at_ms=row["expires_at_ms"],
                consumed_at_ms=row["consumed_at_ms"],
                created_at_ms=row["created_at_ms"],
            )
            for row in rows
        )

    def consume_approval(self, approval_id: str, *, now_ms: int) -> bool:
        """CAS-mark one approval consumed; False when already consumed."""

        def _consume(conn):
            cursor = conn.execute(
                """UPDATE transaction_approvals
                       SET consumed_at_ms = ?
                     WHERE approval_id = ? AND consumed_at_ms IS NULL""",
                (now_ms, approval_id),
            )
            return cursor.rowcount == 1

        return self._db._execute_write(_consume)

    # ── Events ───────────────────────────────────────────────────────

    def _insert_event_row(
        self,
        conn,
        *,
        transaction_id: str,
        kind: str,
        effect_id: Optional[str],
        payload: Mapping[str, Any],
        idempotency_key: str,
        created_at_ms: int,
    ):
        existing = conn.execute(
            """SELECT * FROM transaction_events
                WHERE transaction_id = ? AND idempotency_key = ?""",
            (transaction_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            return existing
        count = conn.execute(
            "SELECT COUNT(*) FROM transaction_events WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()[0]
        # Zero-padded per-transaction sequence keeps (created_at_ms,
        # event_id) ordering total even for same-millisecond events.
        event_id = f"{transaction_id}:{count + 1:08d}:{uuid.uuid4().hex[:8]}"
        conn.execute(
            """INSERT INTO transaction_events (
                   event_id, transaction_id, kind, effect_id, payload_json,
                   idempotency_key, created_at_ms
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (event_id, transaction_id, kind, effect_id,
             canonical_json(dict(payload)), idempotency_key, created_at_ms),
        )
        return conn.execute(
            "SELECT * FROM transaction_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()

    def append_event(
        self,
        transaction_id: str,
        kind: str,
        *,
        payload: Optional[Mapping[str, Any]] = None,
        effect_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> TransactionEvent:
        if not kind:
            raise ValueError("event kind is required")
        key = idempotency_key or f"{kind}:{uuid.uuid4().hex}"
        now = self._now_ms()

        def _append(conn):
            return self._insert_event_row(
                conn,
                transaction_id=transaction_id,
                kind=kind,
                effect_id=effect_id,
                payload=payload or {},
                idempotency_key=key,
                created_at_ms=now,
            )

        return self._event_record(self._db._execute_write(_append))

    # ── Snapshot ─────────────────────────────────────────────────────

    def load_snapshot(self, transaction_id: str) -> Optional[TransactionSnapshot]:
        transaction = self.get_transaction(transaction_id)
        if transaction is None:
            return None

        def _read(conn):
            revisions = conn.execute(
                """SELECT revision FROM transaction_revisions
                    WHERE transaction_id = ? ORDER BY revision""",
                (transaction_id,),
            ).fetchall()
            effects = conn.execute(
                """SELECT * FROM transaction_effects
                    WHERE transaction_id = ?
                    ORDER BY revision, node_id, effect_id""",
                (transaction_id,),
            ).fetchall()
            events = conn.execute(
                """SELECT * FROM transaction_events
                    WHERE transaction_id = ?
                    ORDER BY created_at_ms, event_id""",
                (transaction_id,),
            ).fetchall()
            return revisions, effects, events

        revision_rows, effect_rows, event_rows = self._db._execute_read(_read)
        revisions = tuple(
            self.get_revision(transaction_id, row["revision"])
            for row in revision_rows
        )
        return TransactionSnapshot(
            transaction=transaction,
            revisions=revisions,
            effects=tuple(self._effect_record(row) for row in effect_rows),
            events=tuple(self._event_record(row) for row in event_rows),
        )
