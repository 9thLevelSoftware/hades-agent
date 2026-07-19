"""Typed profile-local persistence for the Preferences & Autonomy Center.

:class:`AutonomyStore` is a facade over ``SessionDB`` (``state.db``) that
owns the runtime half of authority: immutable compiled contract versions,
revision-guarded runtime rules (learned suggestions and temporary
mandates — durable user assertions live in ``config.yaml``), an
append-only rule event log, the decision audit, atomic mandate
consumption with replay idempotency, and the bounded budget ledger.

Persistence rules:

- Every multi-row mutation happens inside a single
  ``SessionDB._execute_write()`` transaction (``BEGIN IMMEDIATE``).
- Contract versions are content-addressed: ``materialize_contract()`` is
  idempotent per content hash, and reads verify the stored bytes against
  the recorded hash before use (fail closed on tamper/corruption).
- Audit rows contain labels, identifiers, and hashes only — never prompt
  text, tool output, secrets, message bodies, or raw recipient values.
- ``hash_recipient()`` uses a random profile-local 32-byte key stored in
  ``state_meta`` as ``autonomy.recipient_hash_key.v1``; it is never
  exported, displayed, or shared across profiles.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Optional, Sequence

from agent.autonomy.canonical import (
    canonical_json,
    content_hash,
    contract_hash,
    hash_recipient as _hash_recipient,
    hash_resource as _hash_resource,
    rule_from_dict,
    rule_to_dict,
)
from agent.autonomy.models import (
    AUTONOMY_CONTRACT_SCHEMA,
    AuthorityDecision,
    AutonomyContract,
    AutonomyRule,
    BudgetReservation,
    ClarificationRequest,
    DecisionStage,
    EvidenceRequirement,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle guard (hades_state → here)
    from hades_state import SessionDB

__all__ = [
    "AutonomyBudgetError",
    "AutonomyIntegrityError",
    "AutonomyStore",
    "AutonomyStoreConflictError",
    "AutonomyStoreError",
    "ConsumptionResult",
    "ContractDraft",
    "DecisionRecord",
    "RuleEvent",
    "StoredContractVersion",
    "StoredRuntimeRule",
]

_RECIPIENT_KEY_META = "autonomy.recipient_hash_key.v1"
_RUNTIME_SOURCES = ("learned_suggestion", "temporary_mandate")

_AUTONOMY_TABLES = (
    "autonomy_contract_versions",
    "autonomy_contract_head",
    "autonomy_runtime_rules",
    "autonomy_rule_events",
    "autonomy_decisions",
    "autonomy_consumptions",
    "autonomy_cost_ledger",
)


class AutonomyStoreError(RuntimeError):
    """Base class for autonomy persistence failures."""


class AutonomyStoreConflictError(AutonomyStoreError):
    """Optimistic-concurrency, replay-identity, or lifecycle conflict."""


class AutonomyIntegrityError(AutonomyStoreError):
    """Stored bytes fail hash/binding verification — fail closed."""


class AutonomyBudgetError(AutonomyStoreError):
    """A budget reservation is negative, duplicate, or over its cap."""


def _now_ms() -> int:
    return int(time.time() * 1000)


# ── Frozen store records ────────────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class ContractDraft:
    """Compiler output awaiting materialization into an immutable version.

    Carries only authorizing content: active user assertions and active
    temporary mandates. Learned suggestions must never appear here.
    """

    profile_id: str
    compiled_at_ms: int
    rules: tuple[AutonomyRule, ...] = ()
    source_fingerprint: str
    schema_id: str = AUTONOMY_CONTRACT_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not self.profile_id.strip():
            raise ValueError("profile_id is required")
        if not isinstance(self.source_fingerprint, str) or not self.source_fingerprint:
            raise ValueError("source_fingerprint is required")
        if isinstance(self.compiled_at_ms, bool) or not isinstance(
            self.compiled_at_ms, int
        ) or self.compiled_at_ms < 0:
            raise ValueError("compiled_at_ms must be a non-negative integer")
        seen: set[str] = set()
        for rule in self.rules:
            if not isinstance(rule, AutonomyRule):
                raise ValueError("rules entries must be AutonomyRule")
            if rule.source == "learned_suggestion":
                raise ValueError(
                    f"rule {rule.rule_id!r} is a learned_suggestion; "
                    "suggestions never enter a compiled contract"
                )
            if rule.state != "active":
                raise ValueError(
                    f"rule {rule.rule_id!r} is {rule.state!r}; only active "
                    "rules belong in a compiled contract"
                )
            if rule.rule_id in seen:
                raise ValueError(f"duplicate rule_id {rule.rule_id!r}")
            seen.add(rule.rule_id)

    def body(self) -> dict[str, Any]:
        return {
            "schema": self.schema_id,
            "profile_id": self.profile_id,
            "compiled_at_ms": self.compiled_at_ms,
            "rules": [rule_to_dict(rule) for rule in self.rules],
        }

    def body_json(self) -> str:
        return canonical_json(self.body())

    def content_hash(self) -> str:
        return contract_hash(self.body())


@dataclass(frozen=True, kw_only=True)
class StoredContractVersion:
    """One immutable, hash-verified contract version row."""

    version: int
    schema_id: str
    content_hash: str
    source_fingerprint: str
    created_at_ms: int
    contract: AutonomyContract


@dataclass(frozen=True, kw_only=True)
class StoredRuntimeRule:
    """A runtime rule row with its optimistic-concurrency revision."""

    rule: AutonomyRule
    revision: int
    updated_at_ms: int

    @property
    def rule_id(self) -> str:
        return self.rule.rule_id

    @property
    def source(self) -> str:
        return self.rule.source

    @property
    def state(self) -> str:
        return self.rule.state

    @property
    def remaining_uses(self) -> Optional[int]:
        return self.rule.remaining_uses

    @property
    def expires_at_ms(self) -> Optional[int]:
        return self.rule.expires_at_ms


@dataclass(frozen=True, kw_only=True)
class RuleEvent:
    """One append-only lifecycle event for a runtime rule."""

    event_id: int
    rule_id: str
    event_type: str
    actor_kind: str
    detail: Mapping[str, Any] = field(default_factory=dict)
    created_at_ms: int


@dataclass(frozen=True, kw_only=True)
class DecisionRecord:
    """An :class:`AuthorityDecision` bound to its operation identity."""

    decision: AuthorityDecision
    operation_key: str
    stage: DecisionStage
    created_at_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.decision, AuthorityDecision):
            raise ValueError("decision must be an AuthorityDecision")
        if not isinstance(self.operation_key, str) or not self.operation_key.strip():
            raise ValueError("operation_key is required")
        if self.stage not in (
            "explain", "preview", "execute", "commit", "compensate",
        ):
            raise ValueError(f"stage {self.stage!r} is not a DecisionStage")
        if isinstance(self.created_at_ms, bool) or not isinstance(
            self.created_at_ms, int
        ) or self.created_at_ms < 0:
            raise ValueError("created_at_ms must be a non-negative integer")

    @property
    def decision_id(self) -> str:
        return self.decision.decision_id

    @property
    def authority_version(self) -> int:
        return self.decision.authority_version

    @property
    def verdict(self) -> str:
        return self.decision.verdict

    @property
    def code(self) -> str:
        return self.decision.code


@dataclass(frozen=True, kw_only=True)
class ConsumptionResult:
    """Outcome of an atomic decision-append + mandate-consumption."""

    decision_id: str
    consumed_rule_ids: tuple[str, ...] = ()
    replayed_decision_id: Optional[str] = None

    @property
    def replayed(self) -> bool:
        return self.replayed_decision_id is not None


# ── Store ───────────────────────────────────────────────────────────────────


class AutonomyStore:
    """Typed autonomy persistence over one profile-local ``SessionDB``."""

    def __init__(self, db: "SessionDB") -> None:
        self._db = db
        self._recipient_key: Optional[bytes] = None

    # ── Keyed hashing ──────────────────────────────────────────────────

    def _hash_key(self) -> bytes:
        """Get or atomically create the profile-local 32-byte hash key."""
        if self._recipient_key is not None:
            return self._recipient_key

        def _get(conn: sqlite3.Connection) -> Optional[str]:
            row = conn.execute(
                "SELECT value FROM state_meta WHERE key = ?",
                (_RECIPIENT_KEY_META,),
            ).fetchone()
            return row[0] if row else None

        value = self._db._execute_read(_get)
        if value is None:
            candidate = secrets.token_bytes(32).hex()

            def _create(conn: sqlite3.Connection) -> str:
                # DO NOTHING keeps the first writer's key under races.
                conn.execute(
                    "INSERT INTO state_meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO NOTHING",
                    (_RECIPIENT_KEY_META, candidate),
                )
                row = conn.execute(
                    "SELECT value FROM state_meta WHERE key = ?",
                    (_RECIPIENT_KEY_META,),
                ).fetchone()
                return row[0]

            value = self._db._execute_write(_create)
        self._recipient_key = bytes.fromhex(value)
        return self._recipient_key

    def hash_recipient(self, value: str) -> str:
        """Keyed profile-local recipient hash (see ``canonical.hash_recipient``)."""
        return _hash_recipient(value, key=self._hash_key())

    def hash_resource(self, value: str) -> str:
        """Keyed profile-local resource-reference hash."""
        return _hash_resource(value, key=self._hash_key())

    # ── Immutable contract versions ────────────────────────────────────

    def materialize_contract(
        self,
        draft: ContractDraft,
        *,
        expected_head_version: Optional[int] = None,
        now_ms: Optional[int] = None,
    ) -> StoredContractVersion:
        """Insert (or reuse) a content-addressed version and set the head.

        Idempotent per content hash. The stored bytes are re-read and
        verified against the hash inside the same transaction, and the
        singleton head row is compare-and-set (``expected_head_version``,
        when given, must match the current head or the call fails with
        no write).
        """
        if not isinstance(draft, ContractDraft):
            raise ValueError("draft must be a ContractDraft")
        body_json = draft.body_json()
        body_hash = draft.content_hash()
        created_at = now_ms if now_ms is not None else _now_ms()

        def _do(conn: sqlite3.Connection) -> tuple[int, int]:
            head_row = conn.execute(
                "SELECT contract_version FROM autonomy_contract_head "
                "WHERE singleton = 1"
            ).fetchone()
            head_version = head_row[0] if head_row else None
            if (
                expected_head_version is not None
                and head_version != expected_head_version
            ):
                raise AutonomyStoreConflictError(
                    f"contract head moved: expected version "
                    f"{expected_head_version}, found {head_version}"
                )
            row = conn.execute(
                "SELECT contract_version, contract_json, created_at_ms "
                "FROM autonomy_contract_versions WHERE content_hash = ?",
                (body_hash,),
            ).fetchone()
            if row is None:
                cursor = conn.execute(
                    "INSERT INTO autonomy_contract_versions "
                    "(schema_id, content_hash, source_fingerprint, "
                    " contract_json, created_at_ms) VALUES (?, ?, ?, ?, ?)",
                    (
                        draft.schema_id,
                        body_hash,
                        draft.source_fingerprint,
                        body_json,
                        created_at,
                    ),
                )
                version = int(cursor.lastrowid)
                stored_json = conn.execute(
                    "SELECT contract_json FROM autonomy_contract_versions "
                    "WHERE contract_version = ?",
                    (version,),
                ).fetchone()[0]
                row_created_at = created_at
            else:
                version = int(row[0])
                stored_json = row[1]
                row_created_at = int(row[2])
            if content_hash(json.loads(stored_json)) != body_hash:
                raise AutonomyIntegrityError(
                    f"stored contract bytes for version {version} do not "
                    f"match content hash {body_hash}"
                )
            conn.execute(
                "INSERT INTO autonomy_contract_head "
                "(singleton, contract_version, content_hash, updated_at_ms) "
                "VALUES (1, ?, ?, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET "
                "contract_version = excluded.contract_version, "
                "content_hash = excluded.content_hash, "
                "updated_at_ms = excluded.updated_at_ms",
                (version, body_hash, created_at),
            )
            return version, row_created_at

        version, row_created_at = self._db._execute_write(_do)
        contract = AutonomyContract(
            schema=draft.schema_id,
            version=version,
            contract_hash=body_hash,
            profile_id=draft.profile_id,
            compiled_at_ms=draft.compiled_at_ms,
            rules=draft.rules,
        )
        return StoredContractVersion(
            version=version,
            schema_id=draft.schema_id,
            content_hash=body_hash,
            source_fingerprint=draft.source_fingerprint,
            created_at_ms=row_created_at,
            contract=contract,
        )

    def _load_contract_row(self, row: sqlite3.Row) -> StoredContractVersion:
        stored_json = row["contract_json"]
        body = json.loads(stored_json)
        if content_hash(body) != row["content_hash"]:
            raise AutonomyIntegrityError(
                f"contract version {row['contract_version']} failed hash "
                "verification; refusing to use tampered/corrupt authority"
            )
        rules = tuple(rule_from_dict(item) for item in body.get("rules", ()))
        contract = AutonomyContract(
            schema=body["schema"],
            version=int(row["contract_version"]),
            contract_hash=row["content_hash"],
            profile_id=body["profile_id"],
            compiled_at_ms=int(body["compiled_at_ms"]),
            rules=rules,
        )
        return StoredContractVersion(
            version=int(row["contract_version"]),
            schema_id=row["schema_id"],
            content_hash=row["content_hash"],
            source_fingerprint=row["source_fingerprint"],
            created_at_ms=int(row["created_at_ms"]),
            contract=contract,
        )

    def get_contract(self, version: int) -> Optional[StoredContractVersion]:
        """Load one immutable version, verifying its hash before use."""

        def _do(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
            return conn.execute(
                "SELECT * FROM autonomy_contract_versions "
                "WHERE contract_version = ?",
                (version,),
            ).fetchone()

        row = self._db._execute_read(_do)
        return self._load_contract_row(row) if row is not None else None

    def get_head(self) -> Optional[StoredContractVersion]:
        """Load the current head version (hash-verified), if any."""

        def _do(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
            return conn.execute(
                "SELECT v.* FROM autonomy_contract_head h "
                "JOIN autonomy_contract_versions v "
                "ON v.contract_version = h.contract_version "
                "WHERE h.singleton = 1"
            ).fetchone()

        row = self._db._execute_read(_do)
        return self._load_contract_row(row) if row is not None else None

    # ── Runtime rules (suggestions + mandates) ─────────────────────────

    def put_runtime_rule(
        self,
        rule: AutonomyRule,
        *,
        expected_revision: int,
        now_ms: Optional[int] = None,
    ) -> StoredRuntimeRule:
        """Insert (``expected_revision=0``) or revision-guarded update.

        Durable user assertions belong in ``config.yaml`` and are
        rejected here. Every transition appends a rule event in the same
        transaction.
        """
        if not isinstance(rule, AutonomyRule):
            raise ValueError("rule must be an AutonomyRule")
        if rule.source not in _RUNTIME_SOURCES:
            raise ValueError(
                f"{rule.source!r} rules are durable config assertions "
                "(user_assertion) and never live in state.db runtime rules"
            )
        updated_at = now_ms if now_ms is not None else _now_ms()
        rule_json = canonical_json(rule_to_dict(rule))
        provenance_json = canonical_json(asdict(rule.provenance))
        remaining = rule.remaining_uses
        if remaining is None and rule.max_uses is not None:
            remaining = rule.max_uses

        def _do(conn: sqlite3.Connection) -> tuple[int, str]:
            row = conn.execute(
                "SELECT revision, state FROM autonomy_runtime_rules "
                "WHERE rule_id = ?",
                (rule.rule_id,),
            ).fetchone()
            if row is None:
                if expected_revision != 0:
                    raise AutonomyStoreConflictError(
                        f"rule {rule.rule_id!r} does not exist; a create "
                        f"requires expected_revision=0 "
                        f"(got {expected_revision})"
                    )
                revision = 1
                conn.execute(
                    "INSERT INTO autonomy_runtime_rules "
                    "(rule_id, source_kind, state, revision, rule_json, "
                    " provenance_json, confidence_ppm, expires_at_ms, "
                    " maximum_uses, remaining_uses, created_at_ms, "
                    " updated_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        rule.rule_id, rule.source, rule.state, revision,
                        rule_json, provenance_json,
                        rule.provenance.confidence_ppm, rule.expires_at_ms,
                        rule.max_uses, remaining, rule.created_at_ms,
                        updated_at,
                    ),
                )
                event_type = "created"
            else:
                if int(row["revision"]) != expected_revision:
                    raise AutonomyStoreConflictError(
                        f"rule {rule.rule_id!r} is at revision "
                        f"{row['revision']}, not {expected_revision}"
                    )
                revision = expected_revision + 1
                cursor = conn.execute(
                    "UPDATE autonomy_runtime_rules SET source_kind = ?, "
                    "state = ?, revision = ?, rule_json = ?, "
                    "provenance_json = ?, confidence_ppm = ?, "
                    "expires_at_ms = ?, maximum_uses = ?, "
                    "remaining_uses = ?, updated_at_ms = ? "
                    "WHERE rule_id = ? AND revision = ?",
                    (
                        rule.source, rule.state, revision, rule_json,
                        provenance_json, rule.provenance.confidence_ppm,
                        rule.expires_at_ms, rule.max_uses, remaining,
                        updated_at, rule.rule_id, expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise AutonomyStoreConflictError(
                        f"rule {rule.rule_id!r} changed concurrently"
                    )
                event_type = "updated"
            self._append_event(
                conn,
                rule_id=rule.rule_id,
                event_type=event_type,
                actor_kind=rule.provenance.actor_kind,
                detail={
                    "state": rule.state,
                    "revision": revision,
                    "source": rule.source,
                },
                created_at_ms=updated_at,
            )
            return revision, event_type

        revision, _ = self._db._execute_write(_do)
        return StoredRuntimeRule(rule=rule, revision=revision, updated_at_ms=updated_at)

    @staticmethod
    def _append_event(
        conn: sqlite3.Connection,
        *,
        rule_id: str,
        event_type: str,
        actor_kind: str,
        detail: Mapping[str, Any],
        created_at_ms: int,
    ) -> None:
        conn.execute(
            "INSERT INTO autonomy_rule_events "
            "(rule_id, event_type, actor_kind, detail_json, created_at_ms) "
            "VALUES (?, ?, ?, ?, ?)",
            (rule_id, event_type, actor_kind, canonical_json(dict(detail)),
             created_at_ms),
        )

    @staticmethod
    def _runtime_rule_from_row(row: sqlite3.Row) -> StoredRuntimeRule:
        data = json.loads(row["rule_json"])
        # Live lifecycle columns are authoritative over the stored JSON.
        data["state"] = row["state"]
        data["remaining_uses"] = row["remaining_uses"]
        data["expires_at_ms"] = row["expires_at_ms"]
        rule = rule_from_dict(data)
        return StoredRuntimeRule(
            rule=rule,
            revision=int(row["revision"]),
            updated_at_ms=int(row["updated_at_ms"]),
        )

    def get_runtime_rule(self, rule_id: str) -> Optional[StoredRuntimeRule]:
        def _do(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
            return conn.execute(
                "SELECT * FROM autonomy_runtime_rules WHERE rule_id = ?",
                (rule_id,),
            ).fetchone()

        row = self._db._execute_read(_do)
        return self._runtime_rule_from_row(row) if row is not None else None

    def list_runtime_rules(
        self,
        *,
        source: Optional[str] = None,
        states: Optional[Sequence[str]] = None,
    ) -> tuple[StoredRuntimeRule, ...]:
        query = "SELECT * FROM autonomy_runtime_rules"
        clauses: list[str] = []
        params: list[Any] = []
        if source is not None:
            clauses.append("source_kind = ?")
            params.append(source)
        if states:
            clauses.append(
                "state IN (%s)" % ",".join("?" for _ in states)
            )
            params.extend(states)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY rule_id"

        def _do(conn: sqlite3.Connection) -> list[sqlite3.Row]:
            return conn.execute(query, params).fetchall()

        return tuple(
            self._runtime_rule_from_row(row)
            for row in self._db._execute_read(_do)
        )

    def list_rule_events(self, rule_id: str) -> tuple[RuleEvent, ...]:
        def _do(conn: sqlite3.Connection) -> list[sqlite3.Row]:
            return conn.execute(
                "SELECT * FROM autonomy_rule_events WHERE rule_id = ? "
                "ORDER BY event_id",
                (rule_id,),
            ).fetchall()

        return tuple(
            RuleEvent(
                event_id=int(row["event_id"]),
                rule_id=row["rule_id"],
                event_type=row["event_type"],
                actor_kind=row["actor_kind"],
                detail=json.loads(row["detail_json"]),
                created_at_ms=int(row["created_at_ms"]),
            )
            for row in self._db._execute_read(_do)
        )

    # ── Decisions, consumption, replay ─────────────────────────────────

    @staticmethod
    def _verify_contract_binding(
        conn: sqlite3.Connection, record: DecisionRecord
    ) -> None:
        row = conn.execute(
            "SELECT content_hash FROM autonomy_contract_versions "
            "WHERE contract_version = ?",
            (record.decision.authority_version,),
        ).fetchone()
        if row is None:
            raise AutonomyIntegrityError(
                f"decision {record.decision_id!r} binds unknown contract "
                f"version {record.decision.authority_version}"
            )
        if row[0] != record.decision.authority_hash:
            raise AutonomyIntegrityError(
                f"decision {record.decision_id!r} binds contract hash "
                f"{record.decision.authority_hash} but version "
                f"{record.decision.authority_version} has {row[0]}"
            )

    @staticmethod
    def _decision_explanation_json(decision: AuthorityDecision) -> str:
        clarification = (
            asdict(decision.clarification)
            if decision.clarification is not None
            else None
        )
        reservation = (
            asdict(decision.budget_reservation)
            if decision.budget_reservation is not None
            else None
        )
        return canonical_json(
            {
                "reason": decision.reason,
                "edit_targets": list(decision.edit_targets),
                "clarification": clarification,
                "expires_at_ms": decision.expires_at_ms,
                "budget_reservation": reservation,
            }
        )

    def _insert_decision(
        self, conn: sqlite3.Connection, record: DecisionRecord
    ) -> None:
        decision = record.decision
        try:
            conn.execute(
                "INSERT INTO autonomy_decisions "
                "(decision_id, operation_key, stage, contract_version, "
                " contract_hash, context_hash, verdict, code, "
                " matched_rule_ids_json, conflicting_rule_ids_json, "
                " required_evidence_json, explanation_json, created_at_ms) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision.decision_id,
                    record.operation_key,
                    record.stage,
                    decision.authority_version,
                    decision.authority_hash,
                    decision.context_hash,
                    decision.verdict,
                    decision.code,
                    canonical_json(list(decision.matched_rule_ids)),
                    canonical_json(list(decision.conflicting_rule_ids)),
                    canonical_json(
                        [asdict(req) for req in decision.required_evidence]
                    ),
                    self._decision_explanation_json(decision),
                    record.created_at_ms,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise AutonomyStoreConflictError(
                f"decision {decision.decision_id!r} conflicts with an "
                f"existing decision or replay identity: {exc}"
            ) from exc

    def record_decision(self, record: DecisionRecord) -> None:
        """Append one decision after verifying its contract binding."""
        if not isinstance(record, DecisionRecord):
            raise ValueError("record must be a DecisionRecord")

        def _do(conn: sqlite3.Connection) -> None:
            self._verify_contract_binding(conn, record)
            self._insert_decision(conn, record)

        self._db._execute_write(_do)

    def _find_replay(
        self, conn: sqlite3.Connection, record: DecisionRecord
    ) -> Optional[str]:
        row = conn.execute(
            "SELECT decision_id FROM autonomy_decisions "
            "WHERE operation_key = ? AND stage = ? "
            "AND contract_version = ? AND context_hash = ?",
            (
                record.operation_key,
                record.stage,
                record.decision.authority_version,
                record.decision.context_hash,
            ),
        ).fetchone()
        return row[0] if row else None

    def consume_rules_and_record_decision(
        self,
        record: DecisionRecord,
        rule_ids: Iterable[str],
    ) -> ConsumptionResult:
        """Atomically append an allow decision and consume its mandates.

        Replay-safe: the unique identity
        ``(operation_key, stage, contract_version, context_hash)`` is
        checked first — a replay returns the original decision ID and
        consumes nothing. All selected mandates must be active,
        unexpired, and unexhausted at the decision timestamp or the whole
        transaction fails with no partial writes.
        """
        if not isinstance(record, DecisionRecord):
            raise ValueError("record must be a DecisionRecord")
        selected = tuple(rule_ids)
        if selected and record.decision.verdict != "allow":
            raise ValueError(
                "only an allow decision may consume mandates "
                f"(got verdict {record.decision.verdict!r})"
            )

        def _do(conn: sqlite3.Connection) -> ConsumptionResult:
            replayed = self._find_replay(conn, record)
            if replayed is not None:
                return ConsumptionResult(
                    decision_id=replayed,
                    consumed_rule_ids=(),
                    replayed_decision_id=replayed,
                )
            self._verify_contract_binding(conn, record)
            rows: dict[str, sqlite3.Row] = {}
            for rule_id in selected:
                row = conn.execute(
                    "SELECT * FROM autonomy_runtime_rules WHERE rule_id = ?",
                    (rule_id,),
                ).fetchone()
                if row is None:
                    raise AutonomyStoreConflictError(
                        f"mandate {rule_id!r} does not exist"
                    )
                if row["source_kind"] != "temporary_mandate":
                    raise AutonomyStoreConflictError(
                        f"rule {rule_id!r} is a {row['source_kind']}; only "
                        "temporary mandates are consumable"
                    )
                if row["state"] != "active":
                    raise AutonomyStoreConflictError(
                        f"mandate {rule_id!r} is {row['state']!r}, not active"
                    )
                expires = row["expires_at_ms"]
                if expires is not None and expires <= record.created_at_ms:
                    raise AutonomyStoreConflictError(
                        f"mandate {rule_id!r} expired at {expires}"
                    )
                remaining = row["remaining_uses"]
                if remaining is not None and remaining <= 0:
                    raise AutonomyStoreConflictError(
                        f"mandate {rule_id!r} is exhausted"
                    )
                rows[rule_id] = row
            self._insert_decision(conn, record)
            for rule_id, row in rows.items():
                conn.execute(
                    "INSERT INTO autonomy_consumptions "
                    "(rule_id, operation_key, stage, decision_id, "
                    " consumed_at_ms) VALUES (?, ?, ?, ?, ?)",
                    (
                        rule_id,
                        record.operation_key,
                        record.stage,
                        record.decision_id,
                        record.created_at_ms,
                    ),
                )
                remaining = row["remaining_uses"]
                new_remaining = remaining - 1 if remaining is not None else None
                new_state = (
                    "consumed" if new_remaining == 0 else row["state"]
                )
                conn.execute(
                    "UPDATE autonomy_runtime_rules SET remaining_uses = ?, "
                    "state = ?, revision = revision + 1, updated_at_ms = ? "
                    "WHERE rule_id = ?",
                    (new_remaining, new_state, record.created_at_ms, rule_id),
                )
                self._append_event(
                    conn,
                    rule_id=rule_id,
                    event_type="consumed",
                    actor_kind="system",
                    detail={
                        "decision_id": record.decision_id,
                        "operation_key": record.operation_key,
                        "stage": record.stage,
                        "remaining_uses": new_remaining,
                        "state": new_state,
                    },
                    created_at_ms=record.created_at_ms,
                )
            return ConsumptionResult(
                decision_id=record.decision_id,
                consumed_rule_ids=selected,
                replayed_decision_id=None,
            )

        return self._db._execute_write(_do)

    def get_decision(self, decision_id: str) -> Optional[DecisionRecord]:
        def _do(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
            return conn.execute(
                "SELECT * FROM autonomy_decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()

        row = self._db._execute_read(_do)
        return self._decision_from_row(row) if row is not None else None

    def list_decisions(self, *, limit: int = 100) -> tuple[DecisionRecord, ...]:
        def _do(conn: sqlite3.Connection) -> list[sqlite3.Row]:
            return conn.execute(
                "SELECT * FROM autonomy_decisions "
                "ORDER BY created_at_ms DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()

        return tuple(
            self._decision_from_row(row) for row in self._db._execute_read(_do)
        )

    @staticmethod
    def _decision_from_row(row: sqlite3.Row) -> DecisionRecord:
        explanation = json.loads(row["explanation_json"])
        clarification_data = explanation.get("clarification")
        clarification = (
            ClarificationRequest(
                question=clarification_data["question"],
                choices=tuple(clarification_data.get("choices") or ()),
                code=clarification_data.get("code", ""),
            )
            if clarification_data
            else None
        )
        reservation_data = explanation.get("budget_reservation")
        reservation = (
            BudgetReservation(**reservation_data) if reservation_data else None
        )
        decision = AuthorityDecision(
            decision_id=row["decision_id"],
            verdict=row["verdict"],
            code=row["code"],
            reason=explanation["reason"],
            authority_version=int(row["contract_version"]),
            authority_hash=row["contract_hash"],
            context_hash=row["context_hash"],
            matched_rule_ids=tuple(json.loads(row["matched_rule_ids_json"])),
            conflicting_rule_ids=tuple(
                json.loads(row["conflicting_rule_ids_json"])
            ),
            required_evidence=tuple(
                EvidenceRequirement(**item)
                for item in json.loads(row["required_evidence_json"])
            ),
            clarification=clarification,
            expires_at_ms=explanation.get("expires_at_ms"),
            edit_targets=tuple(explanation.get("edit_targets") or ()),
            budget_reservation=reservation,
        )
        return DecisionRecord(
            decision=decision,
            operation_key=row["operation_key"],
            stage=row["stage"],
            created_at_ms=int(row["created_at_ms"]),
        )

    # ── Budget ledger ──────────────────────────────────────────────────

    def _window_spend_micros(
        self,
        conn: sqlite3.Connection,
        rule_id: str,
        window_started_at_ms: int,
    ) -> int:
        """Held-or-settled total for one rule window.

        Per operation: a ``release`` cancels the hold, a ``settle``
        supersedes its ``reserve``, otherwise the ``reserve`` counts.
        """
        rows = conn.execute(
            "SELECT operation_key, kind, amount_micros "
            "FROM autonomy_cost_ledger "
            "WHERE rule_id = ? AND window_started_at_ms = ?",
            (rule_id, window_started_at_ms),
        ).fetchall()
        by_operation: dict[str, dict[str, int]] = {}
        for row in rows:
            by_operation.setdefault(row["operation_key"], {})[row["kind"]] = int(
                row["amount_micros"]
            )
        total = 0
        for kinds in by_operation.values():
            if "release" in kinds:
                continue
            if "settle" in kinds:
                total += kinds["settle"]
            elif "reserve" in kinds:
                total += kinds["reserve"]
        return total

    def window_spend_micros(
        self, rule_id: str, window_started_at_ms: int
    ) -> int:
        return self._db._execute_read(
            lambda conn: self._window_spend_micros(
                conn, rule_id, window_started_at_ms
            )
        )

    def _append_ledger_entry(
        self,
        conn: sqlite3.Connection,
        *,
        kind: str,
        rule_id: str,
        operation_key: str,
        decision_id: str,
        amount_micros: int,
        window_started_at_ms: int,
        created_at_ms: int,
    ) -> str:
        entry_id = f"{operation_key}:{kind}"
        try:
            conn.execute(
                "INSERT INTO autonomy_cost_ledger "
                "(entry_id, rule_id, operation_key, decision_id, kind, "
                " amount_micros, window_started_at_ms, created_at_ms) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    entry_id, rule_id, operation_key, decision_id, kind,
                    amount_micros, window_started_at_ms, created_at_ms,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise AutonomyBudgetError(
                f"operation {operation_key!r} already has a {kind!r} "
                f"ledger entry: {exc}"
            ) from exc
        return entry_id

    def reserve_budget(
        self,
        *,
        rule_id: str,
        operation_key: str,
        decision_id: str,
        amount_micros: int,
        window_started_at_ms: int,
        max_window_micros: Optional[int] = None,
        now_ms: Optional[int] = None,
    ) -> str:
        """Reserve bounded cost before an allow returns.

        Rejects negative, duplicate, or over-limit reservations without
        writing anything.
        """
        if isinstance(amount_micros, bool) or not isinstance(amount_micros, int):
            raise AutonomyBudgetError("amount_micros must be an integer")
        if amount_micros < 0:
            raise AutonomyBudgetError(
                f"reservation amount must be >= 0 (got {amount_micros})"
            )
        created_at = now_ms if now_ms is not None else _now_ms()

        def _do(conn: sqlite3.Connection) -> str:
            if max_window_micros is not None:
                held = self._window_spend_micros(
                    conn, rule_id, window_started_at_ms
                )
                if held + amount_micros > max_window_micros:
                    raise AutonomyBudgetError(
                        f"reservation of {amount_micros} exceeds window cap "
                        f"{max_window_micros} (already held/settled {held})"
                    )
            return self._append_ledger_entry(
                conn,
                kind="reserve",
                rule_id=rule_id,
                operation_key=operation_key,
                decision_id=decision_id,
                amount_micros=amount_micros,
                window_started_at_ms=window_started_at_ms,
                created_at_ms=created_at,
            )

        return self._db._execute_write(_do)

    def settle_budget(
        self,
        *,
        rule_id: str,
        operation_key: str,
        decision_id: str,
        amount_micros: int,
        window_started_at_ms: int,
        now_ms: Optional[int] = None,
    ) -> str:
        """Record the settled cost for a previously reserved operation."""
        if isinstance(amount_micros, bool) or not isinstance(amount_micros, int):
            raise AutonomyBudgetError("amount_micros must be an integer")
        if amount_micros < 0:
            raise AutonomyBudgetError("settled amount must be >= 0")
        created_at = now_ms if now_ms is not None else _now_ms()
        return self._db._execute_write(
            lambda conn: self._append_ledger_entry(
                conn,
                kind="settle",
                rule_id=rule_id,
                operation_key=operation_key,
                decision_id=decision_id,
                amount_micros=amount_micros,
                window_started_at_ms=window_started_at_ms,
                created_at_ms=created_at,
            )
        )

    def release_budget(
        self,
        *,
        rule_id: str,
        operation_key: str,
        decision_id: str,
        window_started_at_ms: int,
        now_ms: Optional[int] = None,
    ) -> str:
        """Release a reservation (aborted/compensated operation)."""
        created_at = now_ms if now_ms is not None else _now_ms()
        return self._db._execute_write(
            lambda conn: self._append_ledger_entry(
                conn,
                kind="release",
                rule_id=rule_id,
                operation_key=operation_key,
                decision_id=decision_id,
                amount_micros=0,
                window_started_at_ms=window_started_at_ms,
                created_at_ms=created_at,
            )
        )

    # ── Audit access ───────────────────────────────────────────────────

    def dump_raw_autonomy_tables(self) -> str:
        """Every raw autonomy row as text — used to prove audit privacy."""

        def _do(conn: sqlite3.Connection) -> str:
            parts: list[str] = []
            for table in _AUTONOMY_TABLES:
                for row in conn.execute(f"SELECT * FROM {table}"):
                    parts.append(f"{table}: {tuple(row)!r}")
            return "\n".join(parts)

        return self._db._execute_read(_do)
