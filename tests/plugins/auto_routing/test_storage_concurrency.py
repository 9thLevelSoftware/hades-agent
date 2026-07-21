"""Independent-connection contention contracts for the routing store."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from pathlib import Path
from typing import Callable

import pytest

from plugins.auto_routing.auto_routing import storage as storage_module
from plugins.auto_routing.auto_routing.evidence import (
    build_feedback_event,
    turn_evidence_id,
)
from plugins.auto_routing.auto_routing.models import (
    RoutingDecision,
    RuntimeKey,
    TaskAssessment,
)
from plugins.auto_routing.auto_routing.storage import (
    ActivationReceipt,
    BudgetExceeded,
    BudgetReservation,
    DecisionCandidate,
    EVIDENCE_OBSERVER_BUSY_TIMEOUT_MS,
    ImmutableRecordConflict,
    RoutingStore,
    RuntimeRoutingPending,
    StoreBusy,
    candidate_id_for,
    connect,
    init_db,
)


def _race_decision() -> tuple[RoutingDecision, tuple[DecisionCandidate, ...]]:
    runtime = RuntimeKey(
        provider="provider-a",
        model="model-a",
        auth_identity="subscription:default",
        credential_pool_identity="pool-a",
        endpoint_identity="endpoint-a",
        api_mode="chat_completions",
        inventory_revision="inventory-1",
    )
    assessment = TaskAssessment(
        complexity=0.5,
        domains=("coding",),
        required_capabilities=("tools",),
        required_modalities=("text",),
        expected_context_tokens=100,
        expected_output_tokens=20,
        quality_sensitivity=0.7,
        reliability_sensitivity=0.7,
        latency_sensitivity=0.2,
        cost_sensitivity=0.2,
        risk_class="moderate",
        confidence=0.9,
    )
    decision = RoutingDecision(
        decision_id="decision-race",
        scope="fresh_session",
        session_id="session-race",
        task_id="task-race",
        operation_id=None,
        task_index=None,
        created_at="2026-01-01T00:00:00Z",
        applied_rule_ids=(),
        assessment=assessment,
        task_facts_hash="a" * 64,
        inventory_revision="inventory-1",
        catalog_revision="catalog-1",
        authority_revision="authority-1",
        policy_revision="policy-1",
        adaptive_revision="adaptive-1",
        eligible_candidates=(runtime.stable_id(),),
        rejected_candidates=(),
        normalized_scoring_inputs=(("quality", 0.7),),
        final_scores=((runtime.stable_id(), 0.7),),
        selected_profile_id="coding",
        selected_runtime=runtime,
        selected_reasoning_effort="medium",
        projection_mode="shadow",
        selection_reason="highest_eligible_score",
        projected_fallback_chain=(),
        safe_default_runtime=runtime,
        safe_default_reasoning_effort="medium",
        classifier_runtime_id=runtime.stable_id(),
        classifier_input_tokens=10,
        classifier_output_tokens=5,
        classifier_cost_usd=0,
        routing_latency_seconds=0.01,
    )
    candidate = DecisionCandidate(
        candidate_id=candidate_id_for(
            "coding",
            "primary",
            0,
            runtime.stable_id(),
        ),
        profile_id="coding",
        target_role="primary",
        target_ordinal=0,
        runtime_id=runtime.stable_id(),
        eligible=True,
        reason_codes=(),
        normalized_scoring_inputs=(("quality", 0.7),),
        final_score=0.7,
    )
    return decision, (candidate,)


def _decision_race_worker(path: str, start_event, output_queue) -> None:
    start_event.wait(10)
    with RoutingStore.open(path=path) as store:
        claim = store.claim_decision_operation(
            scope="fresh_session",
            session_id="session-race",
            operation_id=None,
            task_index=None,
            facts_hash="a" * 64,
            lease_seconds=2,
        )
        if claim.status == "claimed":
            time.sleep(0.25)
            decision, candidates = _race_decision()
            commit = store.commit_decision(
                decision,
                candidates=candidates,
                create_epoch=True,
                claim=claim,
            )
            output_queue.put((commit.status, 1, commit.decision.decision_id))
            return
        decision = store.wait_for_decision_operation(claim, timeout_seconds=3)
        output_queue.put(("replayed", 0, decision.decision_id if decision else None))


def _lease_owner_worker(path: str, ready_queue, release_event) -> None:
    with RoutingStore.open(path=path) as store:
        claim = store.claim_decision_operation(
            scope="fresh_session",
            session_id="session-owned",
            operation_id=None,
            task_index=None,
            facts_hash="b" * 64,
            lease_seconds=0.05,
        )
        ready_queue.put((claim.owner_pid, claim.owner_start_token, claim.status))
        release_event.wait(10)


def _claim_then_crash_worker(path: str) -> None:
    with RoutingStore.open(path=path) as store:
        claim = store.claim_decision_operation(
            scope="fresh_session",
            session_id="session-race",
            operation_id=None,
            task_index=None,
            facts_hash="a" * 64,
            lease_seconds=30,
        )
        if claim.status != "claimed":
            os._exit(97)
        os._exit(17)


def _commit_then_crash_worker(path: str) -> None:
    with RoutingStore.open(path=path) as store:
        decision, candidates = _race_decision()
        claim = store.claim_decision_operation(
            scope=decision.scope,
            session_id=decision.session_id,
            operation_id=decision.operation_id,
            task_index=decision.task_index,
            facts_hash=decision.task_facts_hash,
            lease_seconds=30,
        )
        if claim.status != "claimed":
            os._exit(98)
        store.commit_decision(
            decision,
            candidates=candidates,
            create_epoch=True,
            claim=claim,
        )
        os._exit(19)


def _schema_open_worker(path: str, output_queue) -> None:
    try:
        with RoutingStore.open(path=path) as store:
            output_queue.put((
                "ok",
                store.connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()[0],
            ))
    except BaseException as error:
        output_queue.put(("error", repr(error)))


@pytest.fixture
def store_factory(isolated_home: Path) -> Callable[[], RoutingStore]:
    stores: list[RoutingStore] = []

    def create() -> RoutingStore:
        store = RoutingStore.open()
        stores.append(store)
        return store

    yield create

    for store in stores:
        store.close()


def test_second_connection_gets_bounded_busy_failure(
    store_factory: Callable[[], RoutingStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = store_factory()
    second = store_factory()
    second.connection.execute("PRAGMA busy_timeout=1")
    sleeps: list[float] = []
    monkeypatch.setattr(storage_module.time, "sleep", sleeps.append)

    with first.write_txn():
        with pytest.raises(StoreBusy):
            second.reserve_budget(
                "classifier",
                worst_case_usd=0.10,
                daily_limit_usd=1.00,
                now=0.0,
            )

    assert len(sleeps) == 15
    assert all(0.020 <= delay <= 0.150 for delay in sleeps)
    assert second.daily_budget("classifier", "1970-01-01").committed_usd == 0.0


def test_write_transaction_rolls_back_the_entire_body(
    store_factory: Callable[[], RoutingStore],
) -> None:
    store = store_factory()

    with pytest.raises(RuntimeError, match="abort"):
        with store.write_txn() as connection:
            connection.execute(
                "INSERT INTO authority_revisions "
                "(authority_id, document_json, checksum, created_at) "
                "VALUES ('rolled-back', '{}', 'bad', '2026-01-01T00:00:00Z')"
            )
            raise RuntimeError("abort")

    assert store.read_authority_revision("rolled-back") is None
    with store.write_txn() as connection:
        connection.execute(
            "INSERT INTO authority_revisions "
            "(authority_id, document_json, checksum, created_at) "
            "VALUES ('committed', '{}', "
            "'44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a', "
            "'2026-01-01T00:00:00Z')"
        )
    assert store.read_authority_revision("committed") is not None


def test_nested_write_transaction_rolls_back_only_the_failed_savepoint(
    store_factory: Callable[[], RoutingStore],
) -> None:
    store = store_factory()
    checksum = "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"

    def insert(connection: sqlite3.Connection, authority_id: str) -> None:
        connection.execute(
            "INSERT INTO authority_revisions "
            "(authority_id, document_json, checksum, created_at) "
            "VALUES (?, '{}', ?, '2026-01-01T00:00:00Z')",
            (authority_id, checksum),
        )

    with store.write_txn() as connection:
        insert(connection, "outer-before")
        try:
            with store.write_txn() as nested:
                insert(nested, "inner-rolled-back")
                raise RuntimeError("abort inner")
        except RuntimeError as error:
            assert str(error) == "abort inner"
        insert(connection, "outer-after")
        with store.write_txn() as nested:
            insert(nested, "nested-success")

    persisted = {
        row["authority_id"]
        for row in store.connection.execute(
            "SELECT authority_id FROM authority_revisions"
        )
    }
    assert persisted == {"outer-before", "outer-after", "nested-success"}


def test_schema_locked_error_uses_the_bounded_store_busy_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SchemaLockedConnection:
        in_transaction = False

        def __init__(self) -> None:
            self.attempts = 0

        def execute(self, _sql: str) -> None:
            self.attempts += 1
            raise sqlite3.OperationalError("database schema is locked: main")

    connection = SchemaLockedConnection()
    sleeps: list[float] = []
    monkeypatch.setattr(storage_module.time, "sleep", sleeps.append)

    with pytest.raises(StoreBusy) as exc_info:
        with storage_module.write_txn(connection):  # type: ignore[arg-type]
            pytest.fail("transaction body must not run")

    assert isinstance(exc_info.value.__cause__, sqlite3.OperationalError)
    assert connection.attempts == storage_module.BUSY_MAX_RETRIES + 1
    assert len(sleeps) == storage_module.BUSY_MAX_RETRIES


def test_concurrent_reservations_cannot_oversubscribe_one_daily_budget(
    isolated_home: Path,
) -> None:
    barrier = threading.Barrier(2)

    def reserve() -> BudgetReservation | BaseException:
        store = RoutingStore.open()
        try:
            barrier.wait(timeout=10)
            return store.reserve_budget(
                "classifier",
                worst_case_usd=0.75,
                daily_limit_usd=1.00,
                now=0.0,
            )
        except BaseException as exc:
            return exc
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: reserve(), range(2)))

    assert sum(isinstance(outcome, BudgetReservation) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, BudgetExceeded) for outcome in outcomes) == 1

    verifier = RoutingStore.open()
    try:
        budget = verifier.daily_budget("classifier", "1970-01-01")
        assert budget.reserved_usd == pytest.approx(0.75)
        assert budget.committed_usd <= 1.00
    finally:
        verifier.close()


def test_two_processes_racing_same_session_publish_one_complete_decision(
    tmp_path: Path,
) -> None:
    path = tmp_path / "race" / "state.db"
    context = get_context("spawn")
    start_event = context.Event()
    output_queue = context.Queue()
    processes = [
        context.Process(
            target=_decision_race_worker,
            args=(str(path), start_event, output_queue),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start_event.set()
    results = [output_queue.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    assert sorted(result[0] for result in results) == ["computed", "replayed"]
    assert sum(result[1] for result in results) == 1
    assert {result[2] for result in results} == {"decision-race"}
    with RoutingStore.open(path=path) as store:
        assert store.count_decisions() == 1
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM decision_candidates"
            ).fetchone()[0]
            == 1
        )
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM session_route_bindings"
            ).fetchone()[0]
            == 1
        )
        assert (
            store.connection.execute("SELECT COUNT(*) FROM route_epochs").fetchone()[0]
            == 1
        )


def test_same_process_waiting_claim_cannot_publish_owner_decision(
    tmp_path: Path,
) -> None:
    path = tmp_path / "same-process-race" / "state.db"
    barrier = threading.Barrier(2)
    waiting_attempted = threading.Event()
    decision, candidates = _race_decision()

    def race() -> tuple[str, str, str]:
        with RoutingStore.open(path=path) as store:
            barrier.wait(timeout=10)
            claim = store.claim_decision_operation(
                scope="fresh_session",
                session_id=decision.session_id,
                operation_id=None,
                task_index=None,
                facts_hash=decision.task_facts_hash,
                lease_seconds=2,
            )
            if claim.status == "claimed":
                assert waiting_attempted.wait(timeout=10)
                commit = store.commit_decision(
                    decision,
                    candidates=candidates,
                    create_epoch=True,
                    claim=claim,
                )
                return (claim.status, claim.claim_id, commit.status)

            assert claim.status == "waiting"
            try:
                with pytest.raises(RuntimeRoutingPending):
                    store.commit_decision(
                        decision,
                        candidates=candidates,
                        create_epoch=True,
                        claim=claim,
                    )
            finally:
                waiting_attempted.set()
            replayed = store.wait_for_decision_operation(
                claim,
                timeout_seconds=3,
            )
            assert replayed == decision
            return (claim.status, claim.claim_id, "blocked")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: race(), range(2)))

    assert sorted(result[0] for result in results) == ["claimed", "waiting"]
    assert len({result[1] for result in results}) == 2
    assert sorted(result[2] for result in results) == ["blocked", "computed"]
    with RoutingStore.open(path=path) as store:
        assert store.count_decisions() == 1


def test_live_operation_owner_is_never_stolen_after_lease_expiry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "live-owner" / "state.db"
    context = get_context("spawn")
    ready_queue = context.Queue()
    release_event = context.Event()
    owner = context.Process(
        target=_lease_owner_worker,
        args=(str(path), ready_queue, release_event),
    )
    owner.start()
    owner_pid, owner_start_token, owner_status = ready_queue.get(timeout=10)
    assert owner_status == "claimed"
    time.sleep(0.1)

    try:
        with RoutingStore.open(path=path) as store:
            contender = store.claim_decision_operation(
                scope="fresh_session",
                session_id="session-owned",
                operation_id=None,
                task_index=None,
                facts_hash="b" * 64,
                lease_seconds=1,
            )
            assert contender.status == "waiting"
            assert contender.owner_pid == owner_pid
            assert contender.owner_start_token == owner_start_token
            with pytest.raises(RuntimeRoutingPending):
                store.wait_for_decision_operation(
                    contender,
                    timeout_seconds=0.05,
                )
    finally:
        release_event.set()
        owner.join(timeout=10)
        assert owner.exitcode == 0


def test_dead_incomplete_owner_is_reclaimable_and_facts_conflict_is_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "dead-owner" / "state.db"
    context = get_context("spawn")
    ready_queue = context.Queue()
    release_event = context.Event()
    owner = context.Process(
        target=_lease_owner_worker,
        args=(str(path), ready_queue, release_event),
    )
    owner.start()
    ready_queue.get(timeout=10)
    owner.terminate()
    owner.join(timeout=10)

    with RoutingStore.open(path=path) as store:
        reclaimed = store.claim_decision_operation(
            scope="fresh_session",
            session_id="session-owned",
            operation_id=None,
            task_index=None,
            facts_hash="b" * 64,
            lease_seconds=1,
        )
        assert reclaimed.status == "claimed"
        assert reclaimed.owner_pid != owner.pid

        with pytest.raises(ImmutableRecordConflict, match="facts hash"):
            store.claim_decision_operation(
                scope="fresh_session",
                session_id="session-owned",
                operation_id=None,
                task_index=None,
                facts_hash="c" * 64,
                lease_seconds=1,
            )


def test_process_crash_before_commit_is_reclaimed_and_published_once(
    tmp_path: Path,
) -> None:
    path = tmp_path / "crash-before-commit" / "state.db"
    context = get_context("spawn")
    owner = context.Process(
        target=_claim_then_crash_worker,
        args=(str(path),),
    )
    owner.start()
    owner.join(timeout=20)
    assert owner.exitcode == 17

    decision, candidates = _race_decision()
    with RoutingStore.open(path=path) as store:
        reclaimed = store.claim_decision_operation(
            scope=decision.scope,
            session_id=decision.session_id,
            operation_id=decision.operation_id,
            task_index=decision.task_index,
            facts_hash=decision.task_facts_hash,
            lease_seconds=5,
        )
        assert reclaimed.status == "claimed"
        assert reclaimed.owner_pid != owner.pid
        committed = store.commit_decision(
            decision,
            candidates=candidates,
            create_epoch=True,
            claim=reclaimed,
        )
        assert committed.status == "computed"
        assert store.count_decisions() == 1


def test_process_crash_after_commit_replays_complete_bundle(
    tmp_path: Path,
) -> None:
    path = tmp_path / "crash-after-commit" / "state.db"
    context = get_context("spawn")
    owner = context.Process(
        target=_commit_then_crash_worker,
        args=(str(path),),
    )
    owner.start()
    owner.join(timeout=20)
    assert owner.exitcode == 19

    decision, _candidates = _race_decision()
    with RoutingStore.open(path=path) as store:
        replay_claim = store.claim_decision_operation(
            scope=decision.scope,
            session_id=decision.session_id,
            operation_id=decision.operation_id,
            task_index=decision.task_index,
            facts_hash=decision.task_facts_hash,
            lease_seconds=5,
        )
        assert replay_claim.status == "replayed"
        assert (
            store.wait_for_decision_operation(
                replay_claim,
                timeout_seconds=0,
            )
            == decision
        )
        assert store.count_decisions() == 1
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM decision_candidates"
            ).fetchone()[0]
            == 1
        )
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM session_route_bindings"
            ).fetchone()[0]
            == 1
        )
        assert (
            store.connection.execute("SELECT COUNT(*) FROM route_epochs").fetchone()[0]
            == 1
        )


def test_concurrent_legacy_schema_open_migrates_once_and_both_read(
    tmp_path: Path,
) -> None:
    path = tmp_path / "concurrent-open" / "state.db"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.execute("INSERT INTO schema_meta VALUES ('schema_version', '1')")
    connection.commit()
    connection.close()

    context = get_context("spawn")
    output_queue = context.Queue()
    processes = [
        context.Process(
            target=_schema_open_worker,
            args=(str(path), output_queue),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    results = [output_queue.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    assert [result[0] for result in results] == ["ok", "ok"]
    assert len({result[1] for result in results}) == 1
    with RoutingStore.open(path=path) as store:
        tables = {
            str(row[0])
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "adaptive_profile_revisions",
            "adaptive_profile_states",
            "adaptive_lifecycle_events",
            "adaptive_canary_assignments",
            "adaptive_optimizer_leases",
        } <= tables


def test_readers_keep_last_complete_revision_while_another_writer_is_open(
    store_factory: Callable[[], RoutingStore],
) -> None:
    writer = store_factory()
    reader = store_factory()
    baseline = writer.build_baseline_revision(
        authority_id="authority-1",
        overlay={"profiles": {}},
    )
    writer.publish_revision(baseline, expected_active_id=None)

    with writer.write_txn() as connection:
        connection.execute(
            "INSERT INTO adaptive_revisions "
            "(revision_id, authority_id, parent_revision_id, document_json, "
            "checksum, explanation_json, created_at, complete) "
            "VALUES ('in-progress', 'authority-1', ?, '{}', "
            "'44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a', "
            "'{}', '2026-01-01T00:00:01Z', 0)",
            (baseline.revision_id,),
        )

        assert reader.read_active_revision("authority-1") == baseline


def test_initialized_schema_reopens_for_reads_without_waiting_for_writer(
    store_factory: Callable[[], RoutingStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = store_factory()
    baseline = writer.build_baseline_revision(
        authority_id="authority-1",
        overlay={"profiles": {}},
    )
    writer.publish_revision(baseline, expected_active_id=None)
    reader_connection = connect(writer.path)
    reader_connection.execute("PRAGMA busy_timeout=1")
    sleeps: list[float] = []
    monkeypatch.setattr(storage_module.time, "sleep", sleeps.append)

    try:
        with writer.write_txn():
            init_db(reader_connection)
            reader = RoutingStore(writer.path, reader_connection)
            assert reader.read_active_revision("authority-1") == baseline
        assert sleeps == []
    finally:
        reader_connection.close()


def _race_evidence_parent(path: Path, valid_turn_event):
    base_decision, candidates = _race_decision()
    receipt = ActivationReceipt(
        receipt_id="evidence-race-receipt",
        authority_id=base_decision.authority_revision,
        config_sha="b" * 64,
        inventory_contract_sha="c" * 64,
        inventory_revision=base_decision.inventory_revision,
        adapter_capability_sha="d" * 64,
        created_at="2026-01-01T00:00:00Z",
    )
    decision = RoutingDecision.model_validate({
        **base_decision.model_dump(mode="json"),
        "projection_mode": "active",
        "activation_receipt_id": receipt.receipt_id,
        "activation_config_sha": receipt.config_sha,
        "adapter_capability_sha": receipt.adapter_capability_sha,
    })
    with RoutingStore.open(path=path) as store:
        store.write_activation_receipt(receipt)
        committed = store.commit_decision(
            decision,
            candidates=candidates,
            create_epoch=True,
        )
        assert committed.epoch is not None
        marked_epoch = store.mark_route_epoch_provider_started(
            decision.session_id,
            decision_id=decision.decision_id,
            runtime_id=committed.epoch.runtime_id,
            api_request_id="evidence-race-request",
            started_at="2026-01-01T00:00:01Z",
        )
        event = valid_turn_event.model_copy(update={
            "decision_id": decision.decision_id,
            "session_id": decision.session_id,
            "task_id": decision.task_id,
            "route_epoch_id": marked_epoch.route_epoch_id,
            "runtime_id": marked_epoch.runtime_id,
            "profile_id": decision.selected_profile_id,
            "reasoning_effort": decision.selected_reasoning_effort,
        })
        return event.model_copy(update={
            "evidence_id": turn_evidence_id(event.session_id, event.turn_id),
        })


def test_concurrent_identical_evidence_has_one_row(tmp_path, valid_turn_event):
    path = tmp_path / "race.db"
    event = _race_evidence_parent(path, valid_turn_event)

    def write_once(_index):
        with RoutingStore.open(path=path) as store:
            return store.write_evidence_event(event).status

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(write_once, range(32)))

    assert results.count("inserted") == 1
    assert results.count("replayed") == 31
    with RoutingStore.open(path=path) as store:
        assert store.count_evidence_events() == 1


def test_observer_evidence_write_uses_fail_fast_lock_budget(
    tmp_path,
    valid_turn_event,
):
    path = tmp_path / "observer-evidence-budget.db"
    event = _race_evidence_parent(path, valid_turn_event)
    with (
        RoutingStore.open(path=path) as writer,
        RoutingStore.open(path=path) as observer,
    ):
        prior_timeout = observer.connection.execute(
            "PRAGMA busy_timeout"
        ).fetchone()[0]
        with writer.write_txn():
            started = time.monotonic()
            with pytest.raises(StoreBusy, match="observer evidence write exceeded budget"):
                observer.write_observer_evidence_event(event)
            elapsed = time.monotonic() - started

        assert elapsed < EVIDENCE_OBSERVER_BUSY_TIMEOUT_MS / 1000.0 + 0.75
        assert observer.connection.execute("PRAGMA busy_timeout").fetchone()[0] == (
            prior_timeout
        )


def test_concurrent_identical_feedback_preserves_first_observed_at(
    tmp_path,
    valid_turn_event,
):
    path = tmp_path / "feedback-race.db"
    parent = _race_evidence_parent(path, valid_turn_event)
    with RoutingStore.open(path=path) as store:
        store.write_evidence_event(parent)

    timestamps = [f"2026-07-17T12:01:{index:02d}Z" for index in range(32)]

    def write_once(index):
        candidate = build_feedback_event(
            parent,
            "rating-5",
            observed_at=timestamps[index],
        )
        with RoutingStore.open(path=path) as store:
            return store.write_evidence_event(candidate)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(write_once, range(32)))

    assert sum(result.status == "inserted" for result in results) == 1
    assert {result.status for result in results} <= {"inserted", "replayed"}
    observed = {result.event.observed_at for result in results}
    assert len(observed) == 1
    with RoutingStore.open(path=path) as store:
        assert store.count_evidence_events() == 2
