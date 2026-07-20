"""Mission-aware effect-transaction coordinator.

Task 3 — frozen effect contracts, the in-process adapter registry, and
the coordinator that decides when a tool call needs a durable effect
transaction vs. a plain pass-through.

Public surface (intentionally minimal):
- ``EffectSemantics`` / ``PreparedEffect`` — frozen records the
  coordinator hands to the SessionDB layer.
- ``OperationRequest`` — frozen request record the coordinator hands to
  adapters.
- ``EffectAdapter`` — protocol an adapter implements.
- ``AdapterRegistry`` — registry with duplicate-id rejection and loud
  unknown-id lookup.
- ``CoordinatorBlockedError`` / ``UnknownEffectError`` — distinguished
  failure modes.
- ``build_coordinator(...)`` — factory wiring every dependency (no
  module-level globals; ``mission_loader`` is required, ``clock`` and
  ``operation_id_factory`` are injectable for deterministic tests).

The coordinator never opens workflows.db or imports a global mission
DB; mission state and authority are obtained through the injected
``mission_loader`` callable.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Protocol,
    runtime_checkable,
)


# ── Vocabulary ──────────────────────────────────────────────────────────

# ponytail: small, fixed set; anything outside is rejected at the boundary.
EFFECT_SEMANTIC_KINDS = frozenset({
    "read_only",
    "reversible",
    "compensatable",
    "irreversible",
})


# ── Frozen contracts ────────────────────────────────────────────────────


@dataclass(frozen=True)
class EffectSemantics:
    """Coarse effect kind + reversibility flags the coordinator uses to
    decide approval gating and replay safety."""

    kind: Literal["read_only", "reversible", "compensatable", "irreversible"]
    idempotent: bool
    reconcilable: bool

    def __post_init__(self) -> None:
        if self.kind not in EFFECT_SEMANTIC_KINDS:
            raise ValueError(
                f"effect semantic kind must be one of "
                f"{sorted(EFFECT_SEMANTIC_KINDS)!r}; got {self.kind!r}"
            )


@dataclass(frozen=True)
class OperationRequest:
    """Minimal frozen request the coordinator hands to adapters."""

    tool_name: str
    args: Mapping[str, Any]
    mission_id: Optional[str]
    operation_key: str

    def __post_init__(self) -> None:
        # Spec 6: defensive deep copy so a caller mutating the original
        # mapping (or any nested list) cannot corrupt the frozen record.
        object.__setattr__(self, "args", copy.deepcopy(dict(self.args)))


@dataclass(frozen=True)
class PreparedEffect:
    """Coarse prepare() result the coordinator persists as
    ``prepared``/``preview`` JSON."""

    adapter_id: str
    # Mapping values returned from public contracts must be defensive
    # copies / immutable views as appropriate.
    normalized_args: Mapping[str, Any]
    before: Mapping[str, Any]
    preview: Mapping[str, Any]
    semantics: EffectSemantics
    compensation: Optional[Mapping[str, Any]]

    def __post_init__(self) -> None:
        # Spec 6: defensive deep copy on every nested mapping so a
        # caller mutating the original (or its nested lists) cannot
        # corrupt the frozen record.
        object.__setattr__(
            self, "normalized_args", copy.deepcopy(dict(self.normalized_args))
        )
        object.__setattr__(self, "before", copy.deepcopy(dict(self.before)))
        object.__setattr__(self, "preview", copy.deepcopy(dict(self.preview)))
        if self.compensation is not None:
            object.__setattr__(
                self, "compensation", copy.deepcopy(dict(self.compensation))
            )


# ── Adapter protocol + registry ─────────────────────────────────────────


@runtime_checkable
class EffectAdapter(Protocol):
    """An effect adapter that mediates between a tool handler and the
    durable mission layer.

    ``prepare`` runs before the handler; ``commit`` runs the handler;
    ``verify`` post-validates the handler's result; ``reconcile`` and
    ``compensate`` cover the recovery / rollback paths the coordinator
    triggers when authority is uncertain.
    """

    adapter_id: str

    def prepare(self, request: OperationRequest) -> PreparedEffect: ...

    def commit(self, prepared: PreparedEffect, invoke: Callable[[Mapping[str, Any]], Any]) -> Any: ...

    def verify(self, prepared: PreparedEffect, result: Any) -> Mapping[str, Any]: ...

    def reconcile(self, record: Any) -> Mapping[str, Any]: ...

    def compensate(self, record: Any) -> Mapping[str, Any]: ...


class AdapterRegistry:
    """In-process adapter registry. Rejects duplicate ``adapter_id``
    and unknown-lookups (loud failure beats silent fallback)."""

    def __init__(self) -> None:
        self._adapters: Dict[str, EffectAdapter] = {}

    def register(self, adapter: EffectAdapter) -> None:
        adapter_id = getattr(adapter, "adapter_id", None)
        if not isinstance(adapter_id, str) or not adapter_id:
            raise ValueError("adapter must expose a non-empty str 'adapter_id'")
        if adapter_id in self._adapters:
            raise ValueError(
                f"adapter id {adapter_id!r} already registered"
            )
        self._adapters[adapter_id] = adapter

    def get(self, adapter_id: str) -> EffectAdapter:
        if adapter_id not in self._adapters:
            raise KeyError(f"unknown adapter id: {adapter_id!r}")
        return self._adapters[adapter_id]

    def has(self, adapter_id: str) -> bool:
        return adapter_id in self._adapters

    def all_ids(self) -> List[str]:
        return sorted(self._adapters)


# ── Errors ──────────────────────────────────────────────────────────────


class CoordinatorBlockedError(Exception):
    """Coordinator refused the call before invoking the handler. Used
    for unsupported mutations, expired/revoked authority, missing
    adapter, and similar pre-condition failures."""


class UnknownEffectError(Exception):
    """Coordinator could not determine whether the handler's effect
    landed — invoked on handler timeouts and KeyboardInterrupt. The
    handler ran exactly once; the operation journal / effect tx record
    that the effect is uncertain."""


# ── Coordinator factory + execute() ─────────────────────────────────────


@dataclass(frozen=True)
class _Coordinator:
    mission_loader: Callable[[Optional[str]], Optional[Dict[str, Any]]]
    session_db: Any
    operation_journal: Any
    adapter_registry: AdapterRegistry
    approval_request: Callable[[Mapping[str, Any]], Any]
    review_request: Callable[[Mapping[str, Any]], None]
    clock: Callable[[], float]
    operation_id_factory: Callable[[], str]
    sequence_no_factory: Callable[[Optional[str]], int]
    operation_metadata_loader: Callable[[str], Mapping[str, Any]]


def _default_sequence_no_factory(session_db: Any):
    """Return a callable that computes the next per-mission sequence_no
    using a real SessionDB read — no fake ``sequence_counter`` attribute.

    Spec 1: production SessionDB has no ``sequence_counter``; the
    coordinator must compute the next integer from existing rows via
    ``SELECT COALESCE(MAX(sequence_no),0)+1 FROM effect_transactions
    WHERE mission_id=?``. Empty tables return 1; real storage errors
    raised by ``_execute_read`` propagate so a downed DB surfaces
    immediately instead of silently rolling every new transaction
    onto sequence_no=1 and racing the UNIQUE constraint.

    Fail-closed: a SessionDB lacking the ``_execute_read`` primitive
    raises ``TypeError`` at factory-build time. Silently returning 1
    would race the UNIQUE constraint and stamp every new transaction
    with sequence_no=1 — a quiet storage boundary break.
    """
    if not hasattr(session_db, "_execute_read"):
        raise TypeError(
            "session_db must expose '_execute_read' for the default "
            "sequence_no_factory; inject sequence_no_factory=... for "
            "nonconforming storage"
        )

    def _next(mission_id: Optional[str]) -> int:
        if not mission_id:
            return 1
        # Spec 1: empty table returns 1; real errors propagate so a
        # storage outage is loud, not silent.
        row = session_db._execute_read(
            lambda conn: conn.execute(
                "SELECT COALESCE(MAX(sequence_no),0)+1 AS n "
                "FROM effect_transactions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
        )
        if row is None:
            return 1
        # Row may be sqlite3.Row or tuple-like.
        n = row["n"] if hasattr(row, "__getitem__") and "n" in row.keys() else row[0]
        return int(n)
    return _next


def _default_operation_metadata_loader(adapter_registry: AdapterRegistry):
    """Default metadata loader: lazily delegates to the module-level
    ``tools.registry.registry.get_operation_metadata`` singleton.

    Spec 4: the loader, not the mission, decides which adapter /
    semantic kind governs a tool call. Mission remains the
    authority/permission source. The default wires the production
    tool registry as the source of truth; the import is deferred to
    call-time so importing this module never pulls in
    ``tools.registry`` (which has its own heavy imports). Tests can
    inject an ``operation_metadata_loader=`` override for deterministic
    behavior; the ``adapter_registry`` parameter is unused by the
    default and kept only for signature compatibility.
    """
    del adapter_registry  # unused; signature-compatible shim.

    def _load(tool_name: str) -> Mapping[str, Any]:
        # Local import keeps ``agent.effect_transactions`` import-cheap
        # and avoids a circular import risk during interpreter startup.
        from tools.registry import registry as _tool_registry
        return _tool_registry.get_operation_metadata(tool_name)

    return _load


def build_coordinator(
    *,
    mission_loader: Callable[[Optional[str]], Optional[Dict[str, Any]]],
    session_db: Any,
    operation_journal: Any,
    adapter_registry: AdapterRegistry,
    approval_request: Callable[[Mapping[str, Any]], Any],
    review_request: Callable[[Mapping[str, Any]], None],
    clock: Optional[Callable[[], float]] = None,
    operation_id_factory: Optional[Callable[[], str]] = None,
    sequence_no_factory: Optional[Callable[[Optional[str]], int]] = None,
    operation_metadata_loader: Optional[Callable[[str], Mapping[str, Any]]] = None,
) -> Any:
    """Construct a coordinator wired to all of its dependencies.

    ``mission_loader`` is required and MUST be injected — the coordinator
    never opens a global mission DB. ``clock`` defaults to ``time.time``;
    ``operation_id_factory`` defaults to a UUID4-hex callable;
    ``sequence_no_factory`` defaults to a real-SessionDB read of
    ``COALESCE(MAX(sequence_no),0)+1`` per mission (propagates storage
    errors so a downed DB fails loud);
    ``operation_metadata_loader`` defaults to a lazy delegate to the
    module-level ``tools.registry.registry.get_operation_metadata``
    singleton. All are injectable for deterministic tests.
    """
    import time
    import uuid

    return _Coordinator(
        mission_loader=mission_loader,
        session_db=session_db,
        operation_journal=operation_journal,
        adapter_registry=adapter_registry,
        approval_request=approval_request,
        review_request=review_request,
        clock=clock if clock is not None else time.time,
        operation_id_factory=(
            operation_id_factory
            if operation_id_factory is not None
            else lambda: uuid.uuid4().hex
        ),
        sequence_no_factory=(
            sequence_no_factory
            if sequence_no_factory is not None
            else _default_sequence_no_factory(session_db)
        ),
        operation_metadata_loader=(
            operation_metadata_loader
            if operation_metadata_loader is not None
            else _default_operation_metadata_loader(adapter_registry)
        ),
    )


def _authority_is_active(authority: Optional[Mapping[str, Any]], *, clock: Callable[[], float]) -> bool:
    if not authority:
        return True
    if authority.get("revoked"):
        return False
    if authority.get("valid") is False:
        return False
    expires_at = authority.get("expires_at")
    if isinstance(expires_at, (int, float)) and clock() >= expires_at:
        return False
    return True


def _mission_supports_operation(
    mission: Mapping[str, Any], tool_name: str
) -> Optional[Mapping[str, Any]]:
    if mission.get("kind") == "read_only":
        return None
    operations = mission.get("operations") or {}
    entry = operations.get(tool_name)
    if not entry or not entry.get("allowed"):
        return None
    return entry


def _execute(self: _Coordinator, *, tool_name, args, handler, operation_key, mission_id=None):
    """The coordinator boundary — every trust/authority check lives
    here. Tool-side guards are explicitly avoided."""
    # ── 0. Repeat-key short-circuit ─────────────────────────────────
    # If a previous ``coord.execute`` already settled the operation
    # (confirmed + landed), return the stored result without re-running
    # the handler. A running / dispatched / unknown state, on the other
    # hand, must reconcile via ``adapter.reconcile`` rather than blind-
    # retry.
    #
    # Spec 2: the durable operation_id is the stable ``operation_key``
    # whenever it is non-empty. The injected ``operation_id_factory``
    # is the fallback for empty/invalid keys (UUID by default). Two
    # calls with the same operation_key under default wiring must
    # resolve to the same row and never invoke the handler twice.
    operation_id = (
        operation_key
        if isinstance(operation_key, str) and operation_key
        else self.operation_id_factory()
    )
    prior = self.operation_journal.get(operation_id)
    if prior is not None:
        if prior.state == "confirmed" and prior.effect_disposition == "landed":
            # ponytail: single-shot replay — we hand back the stored
            # raw handler result so callers see the same shape they
            # would have seen inline. ``OperationJournal.terminal_result``
            # does the json.loads for us; a None result here means the
            # stored row is unreadable, which raises CoordinatorBlockedError
            # below rather than silently falling through to the full
            # lifecycle (which would re-invoke the handler).
            stored = self.operation_journal.terminal_result(operation_id)
            if stored is not None:
                return stored
            # confirmed + landed but no decodable result: refuse the
            # repeat rather than fabricate. Falling through to the
            # full lifecycle would re-invoke the handler.
            raise CoordinatorBlockedError(
                f"prior operation {operation_id!r} confirmed but result "
                f"unreadable; refusing to re-invoke handler"
            )
        if prior.state in {"running", "dispatched", "unknown"}:
            # Re-running an in-flight operation: the only safe path is
            # reconcile through the registered adapter. The adapter
            # owns the read model; the coordinator does not invent
            # state.
            #
            # Spec: only a ``disposition == "landed"`` outcome from
            # ``adapter.reconcile`` advances the journal to confirmed.
            # Anything else (``unknown``, missing key, malformed shape)
            # means the adapter did NOT certify the effect — leave the
            # journal in its uncertain state and raise
            # ``CoordinatorBlockedError`` BEFORE the handler is
            # re-invoked. Otherwise a returning handler would double-run
            # a side effect or be silently marked "landed" against an
            # actually-uncertain effect.
            adapter_id = prior.destination
            if adapter_id and self.adapter_registry.has(adapter_id):
                adapter = self.adapter_registry.get(adapter_id)
                outcome = adapter.reconcile(prior)
                # ponytail: only a Mapping outcome is a real reconcile
                # envelope. Anything else (None, dataclass, future
                # shape) is treated as no evidence — disposition is
                # None/unknown and we block rather than guess.
                if isinstance(outcome, Mapping):
                    disposition = outcome.get("disposition")
                else:
                    disposition = None
                if disposition == "landed" and isinstance(outcome, Mapping):
                    self.operation_journal.transition(
                        operation_id,
                        from_states={"running", "dispatched", "unknown"},
                        to_state="confirmed",
                        effect_disposition="landed",
                        result=copy.deepcopy(dict(outcome)),
                    )
                    return outcome
                # Disposition is not "landed" (e.g. "unknown", missing,
                # or some future declared value). Refuse to re-invoke
                # the handler and surface a loud block so the caller
                # can resolve the uncertainty manually.
                self.review_request({
                    "operation_id": operation_id,
                    "adapter_id": adapter_id,
                    "reason": "reconcile_non_landed",
                    "disposition": disposition,
                })
                raise CoordinatorBlockedError(
                    f"prior operation {operation_id!r} in flight; "
                    f"adapter reconcile returned "
                    f"disposition={disposition!r} (not 'landed'); "
                    "refusing to re-invoke handler"
                )
            raise CoordinatorBlockedError(
                f"prior operation {operation_id!r} in flight with no "
                f"registered adapter to reconcile"
            )

    request = OperationRequest(
        tool_name=tool_name,
        args=dict(args),
        mission_id=mission_id,
        operation_key=operation_key,
    )

    # ── 1. Mission lookup ───────────────────────────────────────────
    # The loader is always called, including with mission_id=None, so a
    # caller can supply a default mission context without threading an
    # explicit id through. This keeps the loader the single source of
    # mission truth (no module-global mission DB).
    mission = self.mission_loader(mission_id)

    # No mission: pass through, exactly once.
    if mission is None:
        return handler(dict(args))

    # ── 2. Read-only / unsupported mission mutation ──────────────────
    mission_entry = _mission_supports_operation(mission, tool_name)
    if mission_entry is None:
        # Read-only missions and unsupported mutations share the same
        # code path: the handler runs at most once and no SessionDB
        # effect transaction is created. The distinction is *who*
        # raised the error: for explicit ``read_only`` we return the
        # raw handler result; for unsupported mutations we block before
        # the handler to make the failure auditable.
        if mission.get("kind") == "read_only":
            return handler(dict(args))
        raise CoordinatorBlockedError(
            f"mission {mission.get('mission_id')!r} does not permit "
            f"tool {tool_name!r}"
        )

    # ── 3. Authority check (pre-prepare) ────────────────────────────
    if not _authority_is_active(mission.get("authority"), clock=self.clock):
        raise CoordinatorBlockedError(
            f"mission {mission.get('mission_id')!r} authority not active"
        )

    # ── 4. Adapter lookup ───────────────────────────────────────────
    # Spec 4: the loader is the source of truth for adapter_id /
    # effect_semantic_kind / effect_overrides. Mission
    # ``tool_metadata`` / mission-level ``adapter_id`` are not
    # trusted here — a malicious mission payload cannot influence
    # which adapter runs.
    metadata = self.operation_metadata_loader(tool_name)
    adapter_id = metadata.get("effect_adapter")
    semantic_kind_override = metadata.get("effect_semantic_kind")
    effect_overrides = dict(metadata.get("effect_overrides") or {})
    if not adapter_id or not self.adapter_registry.has(adapter_id):
        raise CoordinatorBlockedError(
            f"no registered adapter for tool {tool_name!r}"
        )
    adapter = self.adapter_registry.get(adapter_id)

    # ── 4b. Validate semantic_kind_override BEFORE any write ────────────
    # Spec 4 part 2: a loader that overrides the semantic kind to a
    # bogus value must not leave a pending journal row or an adapter
    # prepare() invocation behind. Validation runs immediately after
    # the metadata is obtained, BEFORE
    # ``operation_journal.create`` / ``adapter.prepare`` / any write.
    if (
        semantic_kind_override is not None
        and semantic_kind_override != ""
        and semantic_kind_override not in EFFECT_SEMANTIC_KINDS
    ):
        raise CoordinatorBlockedError(
            f"effect_semantic_kind override {semantic_kind_override!r} for "
            f"tool {tool_name!r} is not one of "
            f"{sorted(EFFECT_SEMANTIC_KINDS)!r}"
        )

    # ── 5. Operation-journal create + adapter.prepare ───────────────
    self.operation_journal.create(
        operation_id=operation_id,
        kind="mission_effect",
        destination=adapter_id,
        payload_hash=operation_key,
    )

    try:
        prepared = adapter.prepare(request)
    except Exception as exc:
        # Prepare failed — operation stays pending, journal rows stay
        # clean. Surface as a block so callers don't silently retry.
        self.operation_journal.transition(
            operation_id,
            from_states={"pending"},
            to_state="failed",
            effect_disposition="unknown",
            error=f"prepare_failed: {type(exc).__name__}: {exc}",
        )
        raise CoordinatorBlockedError(
            f"adapter.prepare failed: {type(exc).__name__}: {exc}"
        ) from exc

    # ── 5b. Compute effective semantic kind (override already validated) ─
    # Spec 4 part 2: override beats adapter's prepared kind. The
    # override itself was validated against EFFECT_SEMANTIC_KINDS at
    # step 4b, so ``effective_kind`` is always in the vocabulary here.
    effective_kind = semantic_kind_override or prepared.semantics.kind

    # ── 6. Persist prepared/preview tx ──────────────────────────────
    transaction_id = f"{operation_id}:tx"
    self.session_db.create_effect_transaction(
        transaction_id=transaction_id,
        operation_id=operation_id,
        mission_id=mission.get("mission_id") or "",
        execution_id=mission.get("execution_id"),
        step_id=mission.get("step_id"),
        adapter_id=adapter_id,
        # Spec 1: real per-mission sequence allocation via the injected
        # factory. Production default queries SessionDB._execute_read
        # for ``MAX(sequence_no)+1``; no fake ``sequence_counter``
        # attribute on real SessionDB.
        sequence_no=self.sequence_no_factory(
            mission.get("mission_id") or ""
        ),
        # Spec 4 part 2: persisted semantics reflect the effective
        # kind so the durable record matches the approval gate.
        semantics={
            "kind": effective_kind,
            "idempotent": prepared.semantics.idempotent,
            "reconcilable": prepared.semantics.reconcilable,
        },
        depends_on=list(mission.get("depends_on") or []),
        prepared=copy.deepcopy({
            "adapter_id": prepared.adapter_id,
            "normalized_args": prepared.normalized_args,
            "before": prepared.before,
        }),
        preview=copy.deepcopy(prepared.preview),
        compensation=copy.deepcopy(prepared.compensation),
        authority=copy.deepcopy(mission.get("authority")),
    )

    # ── 7. Reload mission authority AFTER preview ───────────────────
    # Spec 7: when an initial mission was loaded, reload authority
    # even if the caller did NOT pass an explicit mission_id. We
    # resolve the mission's own mission_id from the loaded mission
    # dict, then call mission_loader with that resolved id. The
    # no-mission path returns above at step 1, so by definition here
    # ``mission`` is not None.
    resolved_mission_id = mission.get("mission_id") or mission_id
    fresh = self.mission_loader(resolved_mission_id)
    if fresh is not None and not _authority_is_active(
        fresh.get("authority"), clock=self.clock
    ):
        # Authority expired/revoked between prepare and commit. Settle
        # the tx to ``cancelled`` and block the handler.
        self.session_db.transition_effect_transaction(
            transaction_id,
            expected_phase="previewed",
            next_phase="cancelled",
        )
        self.operation_journal.transition(
            operation_id,
            from_states={"pending", "running"},
            to_state="cancelled",
            effect_disposition="none",
            error="authority_revoked_after_prepare",
        )
        raise CoordinatorBlockedError(
            "mission authority expired or revoked after preview"
        )

    # ── 8. Approval gating for irreversible semantics ───────────────
    # ``effective_kind`` was computed at step 5b (override validated
    # against EFFECT_SEMANTIC_KINDS BEFORE the tx row was persisted)
    # so we can simply reuse it here for the approval gate.
    if (
        effective_kind == "irreversible"
        or mission_entry.get("requires_approval")
    ):
        approval_payload = {
            "operation_id": operation_id,
            "transaction_id": transaction_id,
            "tool_name": tool_name,
            "adapter_id": adapter_id,
            "semantics": effective_kind,
            "preview": copy.deepcopy(prepared.preview),
        }
        # Spec 5: a falsy return is a denial. We settle the tx and
        # the journal to ``cancelled`` BEFORE the handler runs, then
        # raise CoordinatorBlockedError. A truthy return (token /
        # approval marker) allows the existing commit path. An
        # exception propagates unchanged without re-invocation.
        approval_result = self.approval_request(approval_payload)
        if not approval_result:
            self.session_db.transition_effect_transaction(
                transaction_id,
                expected_phase="previewed",
                next_phase="cancelled",
            )
            self.operation_journal.transition(
                operation_id,
                from_states={"pending"},
                to_state="cancelled",
                effect_disposition="none",
                error="approval_denied",
            )
            raise CoordinatorBlockedError(
                f"approval denied for operation {operation_id!r}"
            )

    # ── 9. Transition tx to committing; invoke through adapter.commit
    self.session_db.transition_effect_transaction(
        transaction_id,
        expected_phase="previewed",
        next_phase="committing",
    )
    self.operation_journal.transition(
        operation_id,
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )

    try:
        handler_result = adapter.commit(
            prepared,
            invoke=lambda final_args: handler(final_args),
        )
    except (KeyboardInterrupt, TimeoutError) as exc:
        # Interrupts / timeouts become unknown effect — never a retry.
        self._settle_unknown_effect(
            transaction_id=transaction_id,
            operation_id=operation_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        # Surface to caller via injected review hook.
        self.review_request({
            "operation_id": operation_id,
            "transaction_id": transaction_id,
            "reason": f"{type(exc).__name__}",
            "error": str(exc),
        })
        raise UnknownEffectError(
            f"{type(exc).__name__} during commit; effect unknown"
        ) from exc
    except Exception as exc:
        # Other commit failures: settle to ``failed`` so retries can
        # reason about the prior state.
        self.session_db.transition_effect_transaction(
            transaction_id,
            expected_phase="committing",
            next_phase="failed",
        )
        self.operation_journal.transition(
            operation_id,
            from_states={"running"},
            to_state="failed",
            effect_disposition="unknown",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise

    # ── 10. Verify runs while tx is still ``committing`` ───────────
    # The phase graph allows ``committing → unknown_effect | committed
    # | failed`` only. Doing verify under ``committing`` keeps the
    # ``committing → unknown_effect`` transition legal when verify
    # raises — which is the exact path the timeout / interrupt tests
    # exercise.
    try:
        verified = adapter.verify(prepared, handler_result)
    except (KeyboardInterrupt, TimeoutError) as exc:
        self.session_db.transition_effect_transaction(
            transaction_id,
            expected_phase="committing",
            next_phase="unknown_effect",
            compensation={"reason": f"verify_{type(exc).__name__}"},
        )
        self.operation_journal.transition(
            operation_id,
            from_states={"running"},
            to_state="unknown",
            effect_disposition="unknown",
            error=f"verify_{type(exc).__name__}: {exc}",
        )
        self.review_request({
            "operation_id": operation_id,
            "transaction_id": transaction_id,
            "reason": f"verify_{type(exc).__name__}",
            "error": str(exc),
        })
        raise UnknownEffectError(
            f"verify {type(exc).__name__}; effect unknown"
        ) from exc

    # ── 11. Persist raw result + verify envelope; settle committed/confirmed ──
    # Spec 3: the adapter.verify() envelope (the canonical "did the
    # effect land?" shape callers see) is persisted as ``verification``
    # on the tx row alongside the raw handler result. The phase graph
    # still moves ``committing → committed`` — verify simply enriches
    # the row, it does not introduce a new transition.
    self.session_db.transition_effect_transaction(
        transaction_id,
        expected_phase="committing",
        next_phase="committed",
        result=copy.deepcopy(handler_result),
        verification=copy.deepcopy(verified),
    )
    self.operation_journal.transition(
        operation_id,
        from_states={"running"},
        to_state="confirmed",
        effect_disposition="landed",
        result=copy.deepcopy(handler_result),
    )

    # Return the verify-wrapped result so callers see the same
    # envelope whether verify ran inline (normal path) or out-of-band.
    return verified


def _settle_unknown_effect(self, *, transaction_id, operation_id, error):
    self.session_db.transition_effect_transaction(
        transaction_id,
        expected_phase="committing",
        next_phase="unknown_effect",
        compensation={"reason": error},
    )
    self.operation_journal.transition(
        operation_id,
        from_states={"running"},
        to_state="unknown",
        effect_disposition="unknown",
        error=error,
    )


# Bind the unbound methods so the frozen dataclass can carry them.
_Coordinator.execute = _execute  # type: ignore[attr-defined]
_Coordinator._settle_unknown_effect = _settle_unknown_effect  # type: ignore[attr-defined]


# ponytail: a single re-export point keeps the import surface tidy.
__all__ = [
    "EffectSemantics",
    "OperationRequest",
    "PreparedEffect",
    "EffectAdapter",
    "AdapterRegistry",
    "CoordinatorBlockedError",
    "UnknownEffectError",
    "build_coordinator",
    "EFFECT_SEMANTIC_KINDS",
]