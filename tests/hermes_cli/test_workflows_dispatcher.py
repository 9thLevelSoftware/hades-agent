import json
import sqlite3
from pathlib import Path

import pytest

from agent.operation_journal import OperationJournal
from hades_state import SessionDB
from gateway.mission_outbox import MissionOutboxStore
from hades_cli import kanban_db as kb
from hades_cli import missions_db as mdb
from hades_cli import workflows_db as wfdb
from hades_cli import workflows_dispatcher
from hades_cli.workflows_engine import EngineResult
from hades_cli.workflows_spec import WorkflowSpec


def _switch_spec() -> WorkflowSpec:
    return WorkflowSpec.model_validate({
        "id": "demo", "name": "Demo", "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {
            "start": {"type": "pass", "output": {"score": "${ input.score }"}},
            "route": {"type": "switch", "cases": [
                {"name": "high", "when": {"op": "gte", "left": {"path": "$.node.start.output.score"}, "right": 0.8}}
            ]},
            "high": {"type": "pass", "output": {"bucket": "high"}},
            "low": {"type": "pass", "output": {"bucket": "low"}},
        },
        "edges": [
            {"from": "start", "to": "route"},
            {"from": "route.high", "to": "high"},
            {"from": "route.default", "to": "low"},
        ],
    })


def _wait_spec() -> WorkflowSpec:
    return WorkflowSpec.model_validate({
        "id": "wait_demo", "name": "Wait Demo", "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {
            "start": {"type": "pass", "output": {"seen": "${ input.value }"}},
            "pause": {"type": "wait", "seconds": 60},
            "done": {"type": "pass", "output": {"after": "wait"}},
        },
        "edges": [
            {"from": "start", "to": "pause"},
            {"from": "pause", "to": "done"},
        ],
    })


def _send_message_spec(
    *,
    target: str = "${ input.target }",
    delay: int = 30,
    retry: dict | None = None,
    catch: str | None = None,
) -> WorkflowSpec:
    notify = {
        "type": "send_message",
        "platform": "local",
        "target": target,
        "message": {"text": "${ input.body }"},
        "not_before_seconds": delay,
    }
    if retry is not None:
        notify["retry"] = retry
    if catch is not None:
        notify["catch"] = catch
    nodes = {"notify": notify}
    if catch is not None:
        nodes[catch] = {
            "type": "pass",
            "output": {"recovered": "${ error.node }", "message": "${ error.message }"},
        }
    return WorkflowSpec.model_validate({
        "id": "send_message_demo", "name": "Send Message Demo", "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": nodes,
    })


def _start_mission_send_execution(
    tmp_path,
    monkeypatch,
    *,
    target: str = "authorized-target",
    allowed_targets: list[str] | None = None,
    allowed_effects: list[str] | None = None,
    expires_at: int | None = 1_000,
    delay: int = 30,
    retry: dict | None = None,
    catch: str | None = None,
) -> tuple[str, str, Path]:
    home = tmp_path / ".hades"
    monkeypatch.setenv("HADES_HOME", str(home))
    wfdb.init_db()
    authority: dict[str, object] = {
        "allowed_effects": allowed_effects if allowed_effects is not None else ["delayed_message"],
        "message_targets": allowed_targets if allowed_targets is not None else ["authorized-target"],
    }
    if expires_at is not None:
        authority["expires_at"] = expires_at
    with wfdb.connect() as conn:
        spec = _send_message_spec(delay=delay, retry=retry, catch=catch)
        wfdb.deploy_definition(conn, spec, created_by="test")
        mission, execution = mdb.create_mission_and_execution(
            conn,
            workflow_id=spec.id,
            objective="send a delayed local test notification",
            constraints=[],
            authority=authority,
            evidence={"checks": ["workflow_succeeded"]},
            input_data={"target": target, "body": "ready"},
            profile="default",
            now=10,
        )
    return mission.mission_id, execution.execution_id, home / "state.db"


def _start_unlinked_send_execution(
    tmp_path,
    monkeypatch,
    *,
    retry: dict | None = None,
    catch: str | None = None,
) -> tuple[str, Path]:
    home = tmp_path / ".hades"
    monkeypatch.setenv("HADES_HOME", str(home))
    wfdb.init_db()
    with wfdb.connect() as conn:
        spec = _send_message_spec(retry=retry, catch=catch)
        wfdb.deploy_definition(conn, spec, created_by="test")
        execution_id = wfdb.start_execution(
            conn,
            spec.id,
            input_data={"target": "authorized-target", "body": "ready"},
            trigger_type="manual",
        )
    return execution_id, home / "state.db"


def _parallel_spec() -> WorkflowSpec:
    return WorkflowSpec.model_validate({
        "id": "parallel_demo", "name": "Parallel Demo", "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {
            "fork": {"type": "parallel"},
            "research": {"type": "pass", "output": {"summary": "r"}},
            "implement": {"type": "pass", "output": {"summary": "i"}},
            "merge": {"type": "join"},
        },
        "edges": [
            {"from": "fork.research", "to": "research"},
            {"from": "fork.implement", "to": "implement"},
            {"from": "research", "to": "merge"},
            {"from": "implement", "to": "merge"},
        ],
    })


def _agent_spec(done_output=None) -> WorkflowSpec:
    return WorkflowSpec.model_validate({
        "id": "agent_demo", "name": "Agent Demo", "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {
            "ask": {
                "type": "agent_task",
                "profile": "worker-profile",
                "title": "Do agent work",
                "prompt": {"task": "${ input.task }"},
                "workspace_kind": "scratch",
                "workspace_path": "workflow-workspace",
                "skills": ["test-driven-development"],
                "max_retries": 2,
                "model_override": "test-model",
                "goal_mode": True,
                "goal_max_turns": 3,
            },
            "done": {"type": "pass", "output": done_output or {"agent": "${ node.ask.output.answer }"}},
        },
        "edges": [{"from": "ask", "to": "done"}],
    })


def _schedule_spec(*, version: int = 1, enabled: bool = True) -> WorkflowSpec:
    return WorkflowSpec.model_validate({
        "id": "scheduled_demo", "name": "Scheduled Demo", "version": version,
        "enabled": enabled,
        "triggers": [{"type": "schedule", "id": "every_minute", "cron": "* * * * *"}],
        "nodes": {"start": {"type": "pass", "output": {"ok": True}}},
    })


def _fail_spec(*, retry=None, catch=None, recover_output=None) -> WorkflowSpec:
    flaky = {"type": "fail", "output": {"reason": "boom"}}
    if retry is not None:
        flaky["retry"] = retry
    if catch is not None:
        flaky["catch"] = catch
    nodes = {"flaky": flaky}
    if catch is not None:
        nodes[catch] = {
            "type": "pass",
            "output": recover_output or {"failed": "${ error.node }"},
        }
    return WorkflowSpec.model_validate({
        "id": "fail_demo", "name": "Fail Demo", "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": nodes,
    })


def _start_execution(tmp_path, monkeypatch, input_data=None) -> str:
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, _switch_spec(), created_by="test")
        return wfdb.start_execution(
            conn,
            "demo",
            input_data={} if input_data is None else input_data,
            trigger_type="manual",
        )


def _start_spec_execution(tmp_path, monkeypatch, spec: WorkflowSpec) -> str:
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, spec, created_by="test")
        return wfdb.start_execution(conn, spec.id, input_data={}, trigger_type="manual")


def _start_agent_spec_execution(tmp_path, monkeypatch, spec: WorkflowSpec) -> str:
    home = tmp_path / ".hades"
    home.mkdir()
    monkeypatch.setenv("HADES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, spec, created_by="test")
        return wfdb.start_execution(conn, spec.id, input_data={}, trigger_type="manual")


def _node_runs(exec_id: str, node_id: str):
    with wfdb.connect() as conn:
        return [dict(row) for row in conn.execute(
            """
            SELECT * FROM workflow_node_runs
             WHERE execution_id = ? AND node_id = ?
             ORDER BY id
            """,
            (exec_id, node_id),
        )]


def _execution_state(exec_id: str):
    with wfdb.connect() as conn:
        execution = wfdb.get_execution(conn, exec_id)
        claim = dict(conn.execute(
            """
            SELECT claim_lock, claim_expires
              FROM workflow_executions
             WHERE execution_id = ?
            """,
            (exec_id,),
        ).fetchone())
        events = [dict(row) for row in conn.execute(
            """
            SELECT kind, payload_json
              FROM workflow_events
             WHERE execution_id = ?
             ORDER BY id
            """,
            (exec_id,),
        )]
    return execution, claim, events


def _set_effect_phase(state_db: SessionDB, transaction_id: str, phase: str) -> None:
    paths = {
        "pending": (),
        "previewed": ("previewed",),
        "committing": ("previewed", "committing"),
        "committed": ("previewed", "committing", "committed"),
        "unknown_effect": ("previewed", "committing", "unknown_effect"),
        "failed": ("failed",),
        "cancelled": ("cancelled",),
    }
    effect = state_db.get_effect_transaction(transaction_id)
    assert effect is not None
    if effect.phase == phase:
        return
    if effect.phase != "pending":
        # Contradiction tests intentionally model durable corruption that cannot
        # be reached through the public phase-transition API.
        state_db._execute_write(
            lambda conn: conn.execute(
                "UPDATE effect_transactions SET phase = ? WHERE transaction_id = ?",
                (phase, transaction_id),
            )
        )
        return
    for next_phase in paths[phase]:
        assert state_db.transition_effect_transaction(
            transaction_id,
            expected_phase=effect.phase,
            next_phase=next_phase,
        )
        effect = state_db.get_effect_transaction(transaction_id)
        assert effect is not None


def _set_outbox_effect_phase(state_db: SessionDB, outbox_id: str, phase: str) -> None:
    outbox = state_db.get_outbox_by_id(outbox_id)
    assert outbox is not None and outbox.transaction_id is not None
    _set_effect_phase(state_db, outbox.transaction_id, phase)


def _terminalize_outbox(
    state_db_path: Path,
    outbox_id: str,
    status: str,
    *,
    effect_phase: str | None = None,
) -> None:
    state_db = SessionDB(db_path=state_db_path)
    try:
        store = MissionOutboxStore(state_db)
        claimed = store.claim(
            now=131,
            owner_id="projection-test",
            lease_seconds=60,
            limit=50,
        )
        target = next((row for row in claimed if row.outbox_id == outbox_id), None)
        assert target is not None
        for row in claimed:
            if row.outbox_id != outbox_id:
                assert store.release(
                    row.outbox_id,
                    owner_id="projection-test",
                    claim_token=row.claim_token,
                )
        if status == "delivered":
            assert store.mark_delivered(
                outbox_id,
                owner_id="projection-test",
                claim_token=target.claim_token,
                result={"message_id": "projection-7"},
            )
        elif status == "unknown":
            assert store.mark_unknown(
                outbox_id,
                owner_id="projection-test",
                claim_token=target.claim_token,
                result={"reason": "router timeout"},
            )
        elif status == "failed":
            assert store.mark_failed(
                outbox_id,
                owner_id="projection-test",
                claim_token=target.claim_token,
                error="router rejected",
            )
        elif status == "cancelled":
            assert store.cancel(
                outbox_id,
                expected_revision=target.revision,
                owner_id="projection-test",
                claim_token=target.claim_token,
            )
        else:
            raise AssertionError(f"unsupported test status: {status}")
        if effect_phase is None and target.transaction_id is not None:
            effect_phase = {
                "delivered": "committed",
                "failed": "failed",
                "cancelled": "cancelled",
                "unknown": "unknown_effect",
            }[status]
        if effect_phase is not None:
            assert target.transaction_id is not None
            _set_effect_phase(state_db, target.transaction_id, effect_phase)
    finally:
        state_db.close()


def test_agent_result_contract_enum_accepts_boolean_values():
    assert workflows_dispatcher._validate_result_contract(
        {"approved": True, "review_required": False},
        {"approved": "true|false", "review_required": "true|false"},
    ) == []


def test_result_contract_rejects_string_for_array():
    assert workflows_dispatcher._validate_result_contract(
        {"sources": "not an array"}, {"sources": "array"}
    ) == ["result key sources must be array"]


def test_result_contract_rejects_string_for_object():
    assert workflows_dispatcher._validate_result_contract(
        {"metadata": "not an object"}, {"metadata": "object"}
    ) == ["result key metadata must be object"]


def test_result_contract_accepts_array_and_object():
    assert workflows_dispatcher._validate_result_contract(
        {"sources": [], "metadata": {}},
        {"sources": "array", "metadata": "object"},
    ) == []


def test_tick_initializes_empty_db_path(tmp_path):
    db_path = tmp_path / "workflows.db"

    assert workflows_dispatcher.tick(db_path=db_path, limit=1) == 0

    with wfdb.connect(db_path) as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        schedules_indexes = {
            row["name"] for row in conn.execute("PRAGMA index_list(workflow_schedules)")
        }
    assert {
        "workflow_definitions",
        "workflow_executions",
        "workflow_node_runs",
        "workflow_events",
        "workflow_schedules",
    } <= tables
    assert "idx_workflow_schedules_enabled" in schedules_indexes


def test_tick_runs_queued_pass_switch_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, _switch_spec(), created_by="test")
        exec_id = wfdb.start_execution(conn, "demo", input_data={"score": 0.9}, trigger_type="manual")

    assert workflows_dispatcher.tick(limit=1) == 1

    with wfdb.connect() as conn:
        execution = wfdb.get_execution(conn, exec_id)
        events = [row["kind"] for row in conn.execute(
            "SELECT kind FROM workflow_events WHERE execution_id = ? ORDER BY id",
            (exec_id,),
        )]
    assert execution.status == "succeeded"
    assert execution.context["node"]["high"]["output"] == {"bucket": "high"}
    assert "execution_started" in events
    assert "node_succeeded" in events
    assert "execution_succeeded" in events


def test_tick_starts_ready_feed_items_before_queued_execution_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    spec = WorkflowSpec.model_validate({
        "id": "intake_demo",
        "name": "Intake Demo",
        "version": 1,
        "triggers": [{
            "type": "manual",
            "id": "kickoff",
            "input": {"mode": "review"},
            "input_schema": {"score": {"kind": "number", "required": True, "default": 0.9}},
            "intake": {"mode": "continuous"},
        }],
        "nodes": {"start": {"type": "pass", "output": {"score": "${ input.score }", "mode": "${ input.mode }"}}},
    })
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, spec, created_by="test")
        feed = wfdb.open_input_feed(conn, "intake_demo", trigger_id="kickoff")
        item = wfdb.enqueue_input_item(conn, feed.feed_id, {})

    assert workflows_dispatcher.tick(limit=1) == 1
    with wfdb.connect() as conn:
        item = wfdb.get_input_item(conn, item.item_id)
        execution = wfdb.get_execution(conn, item.execution_id)
        assert item.status == "running"
        assert execution.status == "queued"
        assert execution.input == {"mode": "review", "score": 0.9}

    assert workflows_dispatcher.tick(limit=1) == 1
    with wfdb.connect() as conn:
        item = wfdb.get_input_item(conn, item.item_id)
        execution = wfdb.get_execution(conn, item.execution_id)
    assert execution.status == "succeeded"
    assert item.status == "succeeded"


def test_list_node_runs_keeps_repeated_event_only_successes(tmp_path, monkeypatch):
    exec_id = _start_spec_execution(tmp_path, monkeypatch, _switch_spec())
    with wfdb.connect() as conn:
        wfdb.append_event(
            conn,
            exec_id,
            "node_succeeded",
            {"node_id": "start", "output": {"n": 1}},
        )
        wfdb.append_event(
            conn,
            exec_id,
            "node_succeeded",
            {"node_id": "start", "output": {"n": 2}},
        )
        runs = [
            run
            for run in wfdb.list_node_runs(conn, exec_id)
            if run["node_id"] == "start"
        ]

    assert [run["output"] for run in runs] == [{"n": 1}, {"n": 2}]
    assert [run["id"] for run in runs] == [None, None]


def test_tick_runs_parallel_join_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        spec = _parallel_spec()
        wfdb.deploy_definition(conn, spec, created_by="test")
        exec_id = wfdb.start_execution(conn, spec.id, input_data={}, trigger_type="manual")

    assert workflows_dispatcher.tick(limit=1) == 1

    with wfdb.connect() as conn:
        execution = wfdb.get_execution(conn, exec_id)

    assert execution.status == "succeeded"
    assert execution.context["branches"]["fork"] == {
        "research": {"summary": "r"},
        "implement": {"summary": "i"},
    }
    assert execution.context["node"]["merge"]["output"]["branches"] == {
        "research": {"summary": "r"},
        "implement": {"summary": "i"},
    }


def test_tick_respects_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, _switch_spec(), created_by="test")
        first = wfdb.start_execution(conn, "demo", input_data={"score": 0.9}, trigger_type="manual")
        second = wfdb.start_execution(conn, "demo", input_data={"score": 0.1}, trigger_type="manual")

    assert workflows_dispatcher.tick(limit=1) == 1

    with wfdb.connect() as conn:
        statuses = {
            exec_id: wfdb.get_execution(conn, exec_id).status
            for exec_id in (first, second)
        }
    assert sorted(statuses.values()) == ["queued", "succeeded"]


def test_tick_prefers_existing_queued_execution_over_new_feed_admission(tmp_path, monkeypatch):
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    spec = WorkflowSpec.model_validate({
        "id": "fair_feed_demo",
        "name": "Fair Feed Demo",
        "version": 1,
        "triggers": [{
            "type": "manual",
            "id": "kickoff",
            "intake": {"mode": "continuous"},
        }],
        "nodes": {"start": {"type": "pass", "output": {"done": True}}},
    })
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, spec, created_by="test")
        manual = wfdb.start_execution(conn, spec.id, input_data={}, trigger_type="manual", now=1)
        feed = wfdb.open_input_feed(conn, spec.id, trigger_id="kickoff", now=2)
        item = wfdb.enqueue_input_item(conn, feed.feed_id, {}, now=3)

    assert workflows_dispatcher.tick(limit=1, now=10) == 1

    with wfdb.connect() as conn:
        assert wfdb.get_execution(conn, manual).status == "succeeded"
        item = wfdb.get_input_item(conn, item.item_id)
        assert item.status == "queued"
        assert item.execution_id is None


def test_tick_counts_schedule_admission_against_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    spec = WorkflowSpec.model_validate({
        "id": "schedule_budget_demo",
        "name": "Schedule Budget Demo",
        "version": 1,
        "triggers": [
            {"type": "schedule", "id": "a", "cron": "* * * * *"},
            {"type": "schedule", "id": "b", "cron": "* * * * *"},
        ],
        "nodes": {"start": {"type": "pass", "output": {"ok": True}}},
    })
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, spec, created_by="test")
        conn.execute("UPDATE workflow_schedules SET next_run_at = 100")

    assert workflows_dispatcher.tick(limit=1, now=100) == 1

    with wfdb.connect() as conn:
        rows = conn.execute(
            "SELECT trigger_id, status FROM workflow_executions ORDER BY created_at, execution_id"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["trigger_id"] in {"a", "b"}
    assert rows[0]["status"] == "succeeded"


def test_unresumable_wait_failure_terminalizes_waiting_node_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    spec = WorkflowSpec.model_validate({
        "id": "unresumable_wait_demo",
        "name": "Unresumable Wait Demo",
        "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {"joiner": {"type": "join"}},
    })
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, spec, created_by="test")
        exec_id = wfdb.start_execution(conn, spec.id, input_data={}, trigger_type="manual")

    def fake_run(*_args, **_kwargs):
        return EngineResult(status="waiting", context={"input": {}, "node": {}}, waiting_nodes=["joiner"])

    monkeypatch.setattr(workflows_dispatcher, "run_in_memory_until_waiting", fake_run)

    assert workflows_dispatcher.tick(limit=1, now=100) == 1

    execution, _claim, events = _execution_state(exec_id)
    runs = _node_runs(exec_id, "joiner")
    assert execution.status == "failed"
    assert runs[0]["status"] == "failed"
    assert "unresumable" in runs[0]["error"]
    assert [event["kind"] for event in events][-1] == "execution_failed"


def test_wait_node_persists_wait_until_then_resumes_when_due(tmp_path, monkeypatch):
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, _wait_spec(), created_by="test")
        exec_id = wfdb.start_execution(
            conn, "wait_demo", input_data={"value": 42}, trigger_type="manual"
        )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1

    execution, claim, events = _execution_state(exec_id)
    assert execution.status == "waiting"
    assert claim == {"claim_lock": None, "claim_expires": None}
    assert [event["kind"] for event in events] == [
        "execution_started",
        "node_succeeded",
        "execution_waiting",
    ]
    with wfdb.connect() as conn:
        pause = conn.execute(
            """
            SELECT * FROM workflow_node_runs
             WHERE execution_id = ? AND node_id = 'pause'
            """,
            (exec_id,),
        ).fetchone()
    assert pause is not None
    assert pause["status"] == "waiting"
    assert pause["wait_until"] == 160

    assert workflows_dispatcher.tick(limit=1, now=161) == 1

    with wfdb.connect() as conn:
        execution = wfdb.get_execution(conn, exec_id)
        pause = conn.execute(
            """
            SELECT * FROM workflow_node_runs
             WHERE execution_id = ? AND node_id = 'pause'
            """,
            (exec_id,),
        ).fetchone()
    assert execution.status == "succeeded"
    assert execution.context["node"]["done"]["output"] == {"after": "wait"}
    assert pause["status"] == "succeeded"
    assert pause["completed_at"] == 161

    execution, claim, events = _execution_state(exec_id)
    assert execution.status == "succeeded"
    assert claim == {"claim_lock": None, "claim_expires": None}
    assert [event["kind"] for event in events] == [
        "execution_started",
        "node_succeeded",
        "execution_waiting",
        "node_succeeded",
        "node_succeeded",
        "execution_succeeded",
    ]
    assert [
        json.loads(event["payload_json"])["node_id"]
        for event in events
        if event["kind"] == "node_succeeded"
    ] == ["start", "pause", "done"]

    assert workflows_dispatcher.tick(limit=1, now=162) == 0
    with wfdb.connect() as conn:
        assert conn.execute(
            """
            SELECT count(*) FROM workflow_node_runs
             WHERE execution_id = ? AND node_id = 'pause'
            """,
            (exec_id,),
        ).fetchone()[0] == 1


def test_send_message_materializes_authorized_outbox_and_waits(tmp_path, monkeypatch):
    mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)

    assert workflows_dispatcher.tick(
        limit=1, now=100, state_db_path=state_db_path
    ) == 1

    execution, claim, events = _execution_state(exec_id)
    runs = _node_runs(exec_id, "notify")
    assert execution.status == "waiting"
    assert claim == {"claim_lock": None, "claim_expires": None}
    assert [event["kind"] for event in events] == [
        "execution_started",
        "execution_waiting",
    ]
    assert len(runs) == 1
    assert runs[0]["status"] == "waiting"
    assert runs[0]["wait_until"] is None
    assert runs[0]["outbox_id"]

    state_db = SessionDB(db_path=state_db_path)
    try:
        outbox = state_db.get_outbox_by_id(runs[0]["outbox_id"])
    finally:
        state_db.close()
    assert outbox is not None
    assert outbox.mission_id == mission_id
    assert outbox.execution_id == exec_id
    assert outbox.node_id == "notify"
    assert outbox.platform == "local"
    assert outbox.target == "authorized-target"
    assert outbox.content == {"text": "ready"}
    assert outbox.not_before == 130
    assert outbox.status == "scheduled"


def test_cancelled_mission_retry_preserves_links_on_validation_failure_and_requeues_once(
    tmp_path, monkeypatch
):
    mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    assert workflows_dispatcher.tick(
        limit=1, now=100, state_db_path=state_db_path
    ) == 1
    outbox_id = _node_runs(exec_id, "notify")[0]["outbox_id"]
    assert outbox_id

    state_db = SessionDB(db_path=state_db_path)
    try:
        store = MissionOutboxStore(state_db)
        current = store.get_by_id(outbox_id)
        assert current is not None
        assert store.cancel(outbox_id, expected_revision=current.revision)
        assert current.transaction_id is not None
        effect_before = state_db.get_effect_transaction(current.transaction_id)
        operation_before = OperationJournal(state_db).get(f"{outbox_id}:operation")
        assert effect_before is not None
        assert effect_before.phase == "cancelled"
        assert operation_before is not None
    finally:
        state_db.close()

    # Model the workflow transaction having rolled back its node-run link while
    # the outbox compensation remains durable and cancelled.
    with wfdb.connect() as conn:
        conn.execute(
            "UPDATE workflow_node_runs SET outbox_id = NULL WHERE execution_id = ? AND node_id = ?",
            (exec_id, "notify"),
        )
        execution = wfdb.get_execution(conn, exec_id)
        bad_context = json.loads(json.dumps(execution.context))
        bad_context["input"]["body"] = "changed after prepare"
        with pytest.raises(workflows_dispatcher._SendMessageMaterializationError):
            workflows_dispatcher._persist_waiting_nodes(
                conn,
                execution_id=exec_id,
                result=EngineResult(
                    status="waiting", context=bad_context, waiting_nodes=["notify"]
                ),
                spec=_send_message_spec(),
                now=150,
                state_db_path=state_db_path,
                workflow_db_path=wfdb.workflows_db_path(),
            )

    state_db = SessionDB(db_path=state_db_path)
    try:
        preserved = state_db.get_outbox_by_id(outbox_id)
        assert preserved is not None
        assert preserved.status == "cancelled"
        assert preserved.transaction_id is not None
        assert state_db.get_effect_transaction(preserved.transaction_id) == effect_before
        assert OperationJournal(state_db).get(f"{outbox_id}:operation") == operation_before
    finally:
        state_db.close()

    with wfdb.connect() as conn:
        execution = wfdb.get_execution(conn, exec_id)
        workflows_dispatcher._persist_waiting_nodes(
            conn,
            execution_id=exec_id,
            result=EngineResult(
                status="waiting", context=execution.context, waiting_nodes=["notify"]
            ),
            spec=_send_message_spec(),
            now=151,
            state_db_path=state_db_path,
            workflow_db_path=wfdb.workflows_db_path(),
        )

    state_db = SessionDB(db_path=state_db_path)
    try:
        requeued = state_db.get_outbox_by_id(outbox_id)
        assert requeued is not None
        assert requeued.status == "scheduled"
        assert requeued.transaction_id is not None
        identity = state_db.get_outbox_by_identity(exec_id, "notify")
        assert identity is not None
        assert identity.outbox_id == outbox_id
        effect_after = state_db.get_effect_transaction(requeued.transaction_id)
        assert effect_after is not None
        assert effect_after.phase == "pending"
        assert effect_after.prepared == effect_before.prepared
        assert effect_after.preview == effect_before.preview
        operation_after = OperationJournal(state_db).get(f"{outbox_id}:operation")
        assert operation_after is not None
        assert operation_after.state == "pending"
        assert operation_after.effect_disposition == "none"
        assert state_db._execute_read(
            lambda conn: conn.execute(
                "SELECT count(*) FROM mission_outbox WHERE execution_id = ? AND node_id = ?",
                (exec_id, "notify"),
            ).fetchone()[0]
        ) == 1
    finally:
        state_db.close()




def test_terminal_outbox_fatal_sibling_overrides_earlier_retry_queue(tmp_path, monkeypatch):
    home = tmp_path / ".hades"
    monkeypatch.setenv("HADES_HOME", str(home))
    wfdb.init_db()
    spec = WorkflowSpec.model_validate(
        {
            "id": "parallel_send_failures",
            "name": "Parallel send failures",
            "version": 1,
            "triggers": [{"type": "manual", "id": "manual"}],
            "nodes": {
                "fork": {"type": "parallel"},
                "a_retry": {
                    "type": "send_message",
                    "platform": "local",
                    "target": "authorized-target",
                    "message": {"text": "retry"},
                    "retry": {"max_attempts": 2, "backoff_seconds": 60},
                },
                "z_fatal": {
                    "type": "send_message",
                    "platform": "local",
                    "target": "authorized-target",
                    "message": {"text": "fatal"},
                },
                "merge": {"type": "join"},
            },
            "edges": [
                {"from": "fork.a_retry", "to": "a_retry"},
                {"from": "fork.z_fatal", "to": "z_fatal"},
                {"from": "a_retry", "to": "merge"},
                {"from": "z_fatal", "to": "merge"},
            ],
        }
    )
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, spec, created_by="test")
        execution_id = wfdb.start_execution(
            conn,
            spec.id,
            input_data={},
            trigger_type="manual",
            now=10,
        )
    with wfdb.connect() as conn:
        conn.execute(
            "UPDATE workflow_executions SET status = 'waiting' WHERE execution_id = ?",
            (execution_id,),
        )
    state_db_path = home / "state.db"
    state_db = SessionDB(db_path=state_db_path)
    try:
        store = MissionOutboxStore(state_db)
        retry = store.materialize(
            execution_id=execution_id,
            node_id="a_retry",
            platform="local",
            target="authorized-target",
            content="retry",
        )
        fatal = store.materialize(
            execution_id=execution_id,
            node_id="z_fatal",
            platform="local",
            target="authorized-target",
            content="fatal",
        )
    finally:
        state_db.close()
    with wfdb.connect() as conn:
        conn.execute(
            """INSERT INTO workflow_node_runs
                   (execution_id, node_id, status, outbox_id)
                 VALUES (?, 'a_retry', 'waiting', ?),
                        (?, 'z_fatal', 'waiting', ?)""",
            (execution_id, retry.outbox_id, execution_id, fatal.outbox_id),
        )
    retry_outbox_id = retry.outbox_id
    fatal_outbox_id = fatal.outbox_id
    assert retry_outbox_id and fatal_outbox_id
    _terminalize_outbox(state_db_path, retry_outbox_id, "failed")
    _terminalize_outbox(state_db_path, fatal_outbox_id, "failed")

    assert workflows_dispatcher.tick(limit=1, now=132, state_db_path=state_db_path) == 0
    execution_state, _, _ = _execution_state(execution_id)
    assert execution_state.status == "failed"
    assert workflows_dispatcher.tick(limit=1, now=133, state_db_path=state_db_path) == 0


def test_conflicting_profile_hint_and_default_home_never_materializes_outbox(
    tmp_path, monkeypatch
):
    _mission_id, execution_id, state_db_path = _start_mission_send_execution(
        tmp_path, monkeypatch
    )
    monkeypatch.setenv("HERMES_PROFILE", "foreign")

    assert workflows_dispatcher.tick(limit=1, now=100, state_db_path=state_db_path) == 0
    state_db = SessionDB(db_path=state_db_path)
    try:
        assert state_db.get_outbox_by_identity(execution_id, "notify") is None
    finally:
        state_db.close()



def test_failed_mission_retry_requeues_fresh_effect_and_claimable_outbox(tmp_path, monkeypatch):
    _mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    assert workflows_dispatcher.tick(limit=1, now=100, state_db_path=state_db_path) == 1
    outbox_id = _node_runs(exec_id, "notify")[0]["outbox_id"]
    assert outbox_id

    state_db = SessionDB(db_path=state_db_path)
    try:
        store = MissionOutboxStore(state_db)
        claimed = store.claim(now=131, owner_id="failed-retry", limit=1)
        assert [row.outbox_id for row in claimed] == [outbox_id]
        assert store.mark_failed(
            outbox_id,
            owner_id="failed-retry",
            claim_token=claimed[0].claim_token,
            error="adapter rejected",
        )
        outbox = store.get_by_id(outbox_id)
        assert outbox is not None and outbox.transaction_id is not None
        assert state_db.transition_effect_transaction(
            outbox.transaction_id, expected_phase="pending", next_phase="failed"
        )
    finally:
        state_db.close()

    with wfdb.connect() as conn:
        conn.execute(
            "UPDATE workflow_node_runs SET outbox_id = NULL WHERE execution_id = ? AND node_id = ?",
            (exec_id, "notify"),
        )
        execution = wfdb.get_execution(conn, exec_id)
        workflows_dispatcher._persist_waiting_nodes(
            conn,
            execution_id=exec_id,
            result=EngineResult(
                status="waiting", context=execution.context, waiting_nodes=["notify"]
            ),
            spec=_send_message_spec(),
            now=151,
            state_db_path=state_db_path,
            workflow_db_path=wfdb.workflows_db_path(),
        )

    state_db = SessionDB(db_path=state_db_path)
    try:
        retry = state_db.get_outbox_by_id(outbox_id)
        assert retry is not None
        assert retry.status == "scheduled"
        assert retry.result is None
        assert retry.transaction_id is not None
        effect = state_db.get_effect_transaction(retry.transaction_id)
        assert effect is not None and effect.phase == "pending"
        operation = OperationJournal(state_db).get(f"{outbox_id}:operation")
        assert operation is not None and operation.state == "pending"
        claimed_again = state_db.claim_due_outbox(151, limit=1)
        assert [row.outbox_id for row in claimed_again] == [outbox_id]
    finally:
        state_db.close()


def test_foreign_profile_terminal_projection_does_not_insert_review(tmp_path, monkeypatch):
    mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    assert workflows_dispatcher.tick(
        limit=1, now=100, state_db_path=state_db_path
    ) == 1
    outbox_id = _node_runs(exec_id, "notify")[0]["outbox_id"]

    state_db = SessionDB(db_path=state_db_path)
    try:
        store = MissionOutboxStore(state_db)
        claimed = store.claim(now=131, owner_id="foreign-profile-test", limit=1)
        assert store.mark_unknown(
            outbox_id,
            owner_id="foreign-profile-test",
            claim_token=claimed[0].claim_token,
            result={"reason": "router timeout after dispatch"},
        )
    finally:
        state_db.close()

    with wfdb.connect() as conn:
        conn.execute(
            "UPDATE missions SET profile = ? WHERE mission_id = ?",
            ("foreign", mission_id),
        )
        before_reviews = conn.execute(
            "SELECT count(*) FROM mission_review_items WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()[0]

    assert workflows_dispatcher.tick(
        limit=1, now=131, state_db_path=state_db_path
    ) == 0

    with wfdb.connect() as conn:
        after_reviews = conn.execute(
            "SELECT count(*) FROM mission_review_items WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()[0]
        assert after_reviews == before_reviews
        assert wfdb.get_execution(conn, exec_id).status == "blocked"


def test_authority_platform_scope_supports_dynamic_plugins_without_cross_scope_leakage():
    from gateway.mission_delivery import _authority_allows_destination as gateway_allows

    authority = {"message_targets": ["irc:42"]}
    for allows in (workflows_dispatcher._authority_allows_destination, gateway_allows):
        assert allows(authority, platform="irc", target="42")
        assert not allows(authority, platform="discord", target="42")
        assert not allows({"message_targets": ["discord:42"]}, platform="irc", target="42")
        assert allows({"message_targets": ["42"]}, platform="irc", target="42")
        assert not allows({"message_targets": ["42"]}, platform="irc", target="discord:42")


def test_delivered_send_message_outbox_resumes_waiting_execution_once(tmp_path, monkeypatch):
    _mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    assert workflows_dispatcher.tick(
        limit=1, now=100, state_db_path=state_db_path
    ) == 1
    outbox_id = _node_runs(exec_id, "notify")[0]["outbox_id"]
    assert outbox_id

    state_db = SessionDB(db_path=state_db_path)
    try:
        store = MissionOutboxStore(state_db)
        claimed = store.claim(
            now=131,
            owner_id="delivery-test",
            lease_seconds=60,
            limit=1,
        )
        assert [row.outbox_id for row in claimed] == [outbox_id]
        assert store.mark_delivered(
            outbox_id,
            owner_id="delivery-test",
            claim_token=claimed[0].claim_token,
            result={
                "message_id": "local-7",
                "secret": "raw-secret-value",
                "stdout": "raw-adapter-output",
            },
        )
        _set_outbox_effect_phase(state_db, outbox_id, "committed")
    finally:
        state_db.close()

    assert workflows_dispatcher.tick(
        limit=1, now=131, state_db_path=state_db_path
    ) == 1
    execution, claim, _events = _execution_state(exec_id)
    runs = _node_runs(exec_id, "notify")
    assert execution.status == "succeeded"
    assert claim == {"claim_lock": None, "claim_expires": None}
    assert len(runs) == 1
    assert runs[0]["status"] == "succeeded"
    assert runs[0]["completed_at"] == 131
    assert json.loads(runs[0]["output_json"]) == {
        "outbox_id": outbox_id,
        "outbox_status": "delivered",
        "result": {
            "message_id": "local-7",
            "secret": "[REDACTED]",
            "stdout": "[REDACTED]",
        },
        "status": "delivered",
    }
    assert "raw-secret-value" not in runs[0]["output_json"]
    assert "raw-adapter-output" not in runs[0]["output_json"]

    assert workflows_dispatcher.tick(
        limit=1, now=132, state_db_path=state_db_path
    ) == 0
    assert len(_node_runs(exec_id, "notify")) == 1


_TERMINAL_OUTBOX_EFFECT_PHASES = {
    "delivered": "committed",
    "failed": "failed",
    "cancelled": "cancelled",
    "unknown": "unknown_effect",
}
_INCOMPATIBLE_TERMINAL_OUTBOX_PHASES = [
    (status, phase)
    for status, required_phase in _TERMINAL_OUTBOX_EFFECT_PHASES.items()
    for phase in (
        "pending",
        "previewed",
        "committing",
        "committed",
        "unknown_effect",
        "failed",
        "cancelled",
    )
    if phase != required_phase
]


@pytest.mark.parametrize(
    ("terminal_status", "effect_phase"),
    _INCOMPATIBLE_TERMINAL_OUTBOX_PHASES,
)
def test_terminal_outbox_effect_phase_contradiction_blocks_projection(
    tmp_path, monkeypatch, terminal_status, effect_phase
):
    mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    assert workflows_dispatcher.tick(limit=1, now=100, state_db_path=state_db_path) == 1
    outbox_id = _node_runs(exec_id, "notify")[0]["outbox_id"]

    _terminalize_outbox(
        state_db_path,
        outbox_id,
        terminal_status,
        effect_phase=effect_phase,
    )

    assert workflows_dispatcher.tick(limit=1, now=131, state_db_path=state_db_path) == 0
    execution, _claim, events = _execution_state(exec_id)
    run = _node_runs(exec_id, "notify")[0]
    assert execution.status == "blocked"
    assert run["status"] == "blocked"
    error = json.loads(run["error"])
    assert error["reason"] == "unknown_effect"
    assert error["reconciliation_required"] is True
    assert "effect phase" in error["identity_mismatch"]
    assert "execution_failed" not in [event["kind"] for event in events]
    with wfdb.connect() as conn:
        mission = mdb.get_mission(conn, mission_id)
        review = conn.execute(
            "SELECT kind, status FROM mission_review_items WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
    assert mission.status == "running"
    assert mission.verdict is None
    assert review is not None
    assert dict(review) == {"kind": "unknown_effect", "status": "pending"}


def test_delivered_outbox_with_committed_effect_phase_succeeds(tmp_path, monkeypatch):
    _mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    assert workflows_dispatcher.tick(limit=1, now=100, state_db_path=state_db_path) == 1
    outbox_id = _node_runs(exec_id, "notify")[0]["outbox_id"]
    _terminalize_outbox(
        state_db_path,
        outbox_id,
        "delivered",
        effect_phase="committed",
    )

    assert workflows_dispatcher.tick(limit=1, now=131, state_db_path=state_db_path) == 1
    execution = _execution_state(exec_id)[0]
    run = _node_runs(exec_id, "notify")[0]
    assert execution.status == "succeeded"
    assert run["status"] == "succeeded"


@pytest.mark.parametrize("terminal_status", ["delivered", "failed", "cancelled", "unknown"])
def test_expected_mission_rejects_ordinary_outbox_for_every_terminal_status(
    tmp_path, monkeypatch, terminal_status
):
    mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    assert workflows_dispatcher.tick(limit=1, now=100, state_db_path=state_db_path) == 1
    outbox_id = _node_runs(exec_id, "notify")[0]["outbox_id"]

    # Model an ordinary workflow outbox attached to a mission execution.  The
    # terminal identity fence must reject this before status-specific handling.
    with sqlite3.connect(state_db_path) as conn:
        conn.execute(
            """
            UPDATE mission_outbox
               SET mission_id = NULL, transaction_id = NULL, status = ?
             WHERE outbox_id = ?
            """,
            (terminal_status, outbox_id),
        )

    assert workflows_dispatcher.tick(limit=1, now=131, state_db_path=state_db_path) == 0
    execution, _claim, events = _execution_state(exec_id)
    run = _node_runs(exec_id, "notify")[0]
    assert execution.status == "blocked"
    assert run["status"] == "blocked"
    assert [item["status"] for item in _node_runs(exec_id, "notify")] == ["blocked"]
    error = json.loads(run["error"])
    assert error["reason"] == "unknown_effect"
    assert error["reconciliation_required"] is True
    assert error["mission_id"] is None
    assert error["expected_mission_id"] == mission_id
    assert "does not match execution mission" in error["identity_mismatch"]
    assert "execution_failed" not in [event["kind"] for event in events]

    with wfdb.connect() as conn:
        mission = mdb.get_mission(conn, mission_id)
        review = conn.execute(
            "SELECT kind, status FROM mission_review_items WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
    assert mission.status == "running"
    assert mission.verdict is None
    assert review is not None
    assert dict(review) == {"kind": "unknown_effect", "status": "pending"}


@pytest.mark.parametrize("corruption", ["missing_effect", "missing_operation", "effect", "operation"])
def test_delivered_mission_outbox_requires_complete_identity_graph(
    tmp_path, monkeypatch, corruption
):
    mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    assert workflows_dispatcher.tick(limit=1, now=100, state_db_path=state_db_path) == 1
    outbox_id = _node_runs(exec_id, "notify")[0]["outbox_id"]
    assert outbox_id

    state_db = SessionDB(db_path=state_db_path)
    try:
        store = MissionOutboxStore(state_db)
        claimed = store.claim(now=131, owner_id="graph-test", limit=1)
        assert len(claimed) == 1
        assert store.mark_delivered(
            outbox_id,
            owner_id="graph-test",
            claim_token=claimed[0].claim_token,
            result={"message_id": "graph-7"},
        )
        outbox = store.get_by_id(outbox_id)
        assert outbox is not None and outbox.transaction_id is not None
        operation_id = f"{outbox_id}:operation"
        if corruption == "missing_effect":
            state_db._execute_write(
                lambda conn: conn.execute(
                    "DELETE FROM effect_transactions WHERE transaction_id = ?",
                    (outbox.transaction_id,),
                )
            )
        elif corruption == "missing_operation":
            state_db._execute_write(
                lambda conn: (
                    conn.execute(
                        "DELETE FROM effect_transactions WHERE transaction_id = ?",
                        (outbox.transaction_id,),
                    ),
                    conn.execute(
                        "DELETE FROM agent_operations WHERE operation_id = ?",
                        (operation_id,),
                    ),
                )
            )
        elif corruption == "effect":
            state_db._execute_write(
                lambda conn: conn.execute(
                    "UPDATE effect_transactions SET prepared_json = ? WHERE transaction_id = ?",
                    (json.dumps({"wrong": True}), outbox.transaction_id),
                )
            )
        else:
            state_db._execute_write(
                lambda conn: conn.execute(
                    "UPDATE agent_operations SET payload_hash = ? WHERE operation_id = ?",
                    ("wrong", operation_id),
                )
            )
    finally:
        state_db.close()

    assert workflows_dispatcher.tick(limit=1, now=131, state_db_path=state_db_path) == 0
    execution, _claim, _events = _execution_state(exec_id)
    run = _node_runs(exec_id, "notify")[0]
    assert execution.status == "blocked"
    assert run["status"] == "blocked"
    error = json.loads(run["error"])
    assert error["reason"] == "unknown_effect"
    assert error["reconciliation_required"] is True
    with wfdb.connect() as conn:
        review = conn.execute(
            "SELECT kind, status FROM mission_review_items WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
    assert review is not None
    assert dict(review) == {"kind": "unknown_effect", "status": "pending"}


@pytest.mark.parametrize("result", [0, False, [], ""])
def test_terminal_outbox_projection_preserves_falsey_result_metadata(tmp_path, monkeypatch, result):
    _mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    assert workflows_dispatcher.tick(limit=1, now=100, state_db_path=state_db_path) == 1
    outbox_id = _node_runs(exec_id, "notify")[0]["outbox_id"]

    state_db = SessionDB(db_path=state_db_path)
    try:
        store = MissionOutboxStore(state_db)
        claimed = store.claim(now=131, owner_id="falsey-test", limit=1)
        assert store.mark_delivered(
            outbox_id,
            owner_id="falsey-test",
            claim_token=claimed[0].claim_token,
            result=result,
        )
        _set_outbox_effect_phase(state_db, outbox_id, "committed")
    finally:
        state_db.close()

    assert workflows_dispatcher.tick(limit=1, now=131, state_db_path=state_db_path) == 1
    output = json.loads(_node_runs(exec_id, "notify")[0]["output_json"])
    assert output["result"] == result


def test_persistence_failure_compensates_preexisting_scheduled_outbox(tmp_path, monkeypatch):
    _mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    assert workflows_dispatcher.tick(limit=1, now=100, state_db_path=state_db_path) == 1
    outbox_id = _node_runs(exec_id, "notify")[0]["outbox_id"]
    assert outbox_id
    with wfdb.connect() as conn:
        conn.execute(
            "UPDATE workflow_node_runs SET outbox_id = NULL WHERE execution_id = ? AND node_id = ?",
            (exec_id, "notify"),
        )
        execution = wfdb.get_execution(conn, exec_id)

        class FailingConnection:
            def execute(self, sql, parameters=()):
                if "UPDATE workflow_node_runs SET outbox_id" in sql:
                    raise sqlite3.IntegrityError("injected node-link failure")
                return conn.execute(sql, parameters)

        with pytest.raises(workflows_dispatcher._SendMessageMaterializationError):
            workflows_dispatcher._persist_waiting_nodes(
                FailingConnection(),
                execution_id=exec_id,
                result=EngineResult(
                    status="waiting", context=execution.context, waiting_nodes=["notify"]
                ),
                spec=_send_message_spec(),
                now=150,
                state_db_path=state_db_path,
                workflow_db_path=wfdb.workflows_db_path(),
            )

    state_db = SessionDB(db_path=state_db_path)
    try:
        orphan = state_db.get_outbox_by_id(outbox_id)
        assert orphan is not None
        assert orphan.status == "cancelled"
        assert state_db.claim_due_outbox(151, limit=10) == []
    finally:
        state_db.close()

    with wfdb.connect() as conn:
        execution = wfdb.get_execution(conn, exec_id)
        workflows_dispatcher._persist_waiting_nodes(
            conn,
            execution_id=exec_id,
            result=EngineResult(
                status="waiting", context=execution.context, waiting_nodes=["notify"]
            ),
            spec=_send_message_spec(),
            now=151,
            state_db_path=state_db_path,
            workflow_db_path=wfdb.workflows_db_path(),
        )

    state_db = SessionDB(db_path=state_db_path)
    try:
        retry = state_db.get_outbox_by_id(outbox_id)
        assert retry is not None and retry.status == "scheduled"
        assert state_db._execute_read(
            lambda conn: conn.execute(
                "SELECT count(*) FROM mission_outbox WHERE execution_id = ? AND node_id = ?",
                (exec_id, "notify"),
            ).fetchone()[0]
        ) == 1
        claimed = state_db.claim_due_outbox(151, limit=10)
        assert [row.outbox_id for row in claimed] == [outbox_id]
    finally:
        state_db.close()


def test_persistence_failure_quarantines_preexisting_claimed_outbox(tmp_path, monkeypatch):
    _mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    assert workflows_dispatcher.tick(limit=1, now=100, state_db_path=state_db_path) == 1
    outbox_id = _node_runs(exec_id, "notify")[0]["outbox_id"]
    assert outbox_id
    state_db = SessionDB(db_path=state_db_path)
    try:
        store = MissionOutboxStore(state_db)
        claimed = store.claim(now=131, owner_id="claimed-orphan", limit=1)
        assert [row.outbox_id for row in claimed] == [outbox_id]
    finally:
        state_db.close()

    with wfdb.connect() as conn:
        conn.execute(
            "UPDATE workflow_node_runs SET outbox_id = NULL WHERE execution_id = ? AND node_id = ?",
            (exec_id, "notify"),
        )
        execution = wfdb.get_execution(conn, exec_id)

        class FailingConnection:
            def execute(self, sql, parameters=()):
                if "UPDATE workflow_node_runs SET outbox_id" in sql:
                    raise sqlite3.IntegrityError("injected node-link failure")
                return conn.execute(sql, parameters)

        with pytest.raises(workflows_dispatcher._SendMessageMaterializationError):
            workflows_dispatcher._persist_waiting_nodes(
                FailingConnection(),
                execution_id=exec_id,
                result=EngineResult(
                    status="waiting", context=execution.context, waiting_nodes=["notify"]
                ),
                spec=_send_message_spec(),
                now=150,
                state_db_path=state_db_path,
                workflow_db_path=wfdb.workflows_db_path(),
            )

    state_db = SessionDB(db_path=state_db_path)
    try:
        quarantined = state_db.get_outbox_by_id(outbox_id)
        assert quarantined is not None
        assert quarantined.status == "unknown"
        assert quarantined.result == {
            "error": "[REDACTED]",
            "reconciliation_required": True,
        }
    finally:
        state_db.close()


def test_failed_send_message_outbox_fails_waiting_execution(tmp_path, monkeypatch):
    _mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    assert workflows_dispatcher.tick(
        limit=1, now=100, state_db_path=state_db_path
    ) == 1
    outbox_id = _node_runs(exec_id, "notify")[0]["outbox_id"]
    assert outbox_id

    state_db = SessionDB(db_path=state_db_path)
    try:
        store = MissionOutboxStore(state_db)
        claimed = store.claim(
            now=131,
            owner_id="delivery-test",
            lease_seconds=60,
            limit=1,
        )
        assert store.mark_failed(
            outbox_id,
            owner_id="delivery-test",
            claim_token=claimed[0].claim_token,
            error="adapter rejected",
        )
        _set_outbox_effect_phase(state_db, outbox_id, "failed")
    finally:
        state_db.close()

    assert workflows_dispatcher.tick(
        limit=1, now=131, state_db_path=state_db_path
    ) == 0
    execution, _claim, _events = _execution_state(exec_id)
    runs = _node_runs(exec_id, "notify")
    assert execution.status == "failed"
    assert runs[0]["status"] == "failed"
    assert "delivery failed" in runs[0]["error"]


def test_unknown_send_message_outbox_fails_waiting_execution_explicitly_for_ordinary_workflow(tmp_path, monkeypatch):
    exec_id, state_db_path = _start_unlinked_send_execution(tmp_path, monkeypatch)
    assert workflows_dispatcher.tick(
        limit=1, now=100, state_db_path=state_db_path
    ) == 1
    outbox_id = _node_runs(exec_id, "notify")[0]["outbox_id"]
    assert outbox_id

    state_db = SessionDB(db_path=state_db_path)
    try:
        store = MissionOutboxStore(state_db)
        claimed = store.claim(
            now=131,
            owner_id="delivery-test",
            lease_seconds=60,
            limit=1,
        )
        assert store.mark_unknown(
            outbox_id,
            owner_id="delivery-test",
            claim_token=claimed[0].claim_token,
            result={"reason": "router timeout after dispatch"},
        )
    finally:
        state_db.close()

    assert workflows_dispatcher.tick(
        limit=1, now=131, state_db_path=state_db_path
    ) == 0
    execution, _claim, _events = _execution_state(exec_id)
    runs = _node_runs(exec_id, "notify")
    assert execution.status == "failed"
    assert runs[0]["status"] == "failed"
    assert "delivery outcome is unknown" in runs[0]["error"]

    state_db = SessionDB(db_path=state_db_path)
    try:
        outbox = state_db.get_outbox_by_id(outbox_id)
    finally:
        state_db.close()
    assert outbox is not None
    assert outbox.status == "unknown"


def test_unknown_send_message_outbox_is_not_retryable_even_with_retry_policy(tmp_path, monkeypatch):
    exec_id, state_db_path = _start_unlinked_send_execution(
        tmp_path,
        monkeypatch,
        retry={"max_attempts": 2, "backoff_seconds": 60},
    )
    assert workflows_dispatcher.tick(
        limit=1, now=100, state_db_path=state_db_path
    ) == 1
    outbox_id = _node_runs(exec_id, "notify")[0]["outbox_id"]

    state_db = SessionDB(db_path=state_db_path)
    try:
        store = MissionOutboxStore(state_db)
        claimed = store.claim(
            now=131,
            owner_id="delivery-test",
            lease_seconds=60,
            limit=1,
        )
        assert store.mark_unknown(
            outbox_id,
            owner_id="delivery-test",
            claim_token=claimed[0].claim_token,
            result={"reason": "router timeout after dispatch"},
        )
    finally:
        state_db.close()

    assert workflows_dispatcher.tick(
        limit=1, now=131, state_db_path=state_db_path
    ) == 0
    execution, _claim, _events = _execution_state(exec_id)
    runs = _node_runs(exec_id, "notify")
    assert execution.status == "failed"
    assert [run["status"] for run in runs] == ["failed"]
    assert "delivery outcome is unknown" in runs[0]["error"]
    assert "unknown_effect" not in runs[0]["error"]


def test_unknown_send_message_outbox_uses_catch_without_retrying(tmp_path, monkeypatch):
    exec_id, state_db_path = _start_unlinked_send_execution(
        tmp_path,
        monkeypatch,
        retry={"max_attempts": 2, "backoff_seconds": 60},
        catch="recover",
    )
    assert workflows_dispatcher.tick(
        limit=1, now=100, state_db_path=state_db_path
    ) == 1
    outbox_id = _node_runs(exec_id, "notify")[0]["outbox_id"]

    state_db = SessionDB(db_path=state_db_path)
    try:
        store = MissionOutboxStore(state_db)
        claimed = store.claim(
            now=131,
            owner_id="delivery-test",
            lease_seconds=60,
            limit=1,
        )
        assert store.mark_unknown(
            outbox_id,
            owner_id="delivery-test",
            claim_token=claimed[0].claim_token,
            result={"reason": "router timeout after dispatch"},
        )
    finally:
        state_db.close()

    assert workflows_dispatcher.tick(
        limit=1, now=131, state_db_path=state_db_path
    ) == 1
    execution, _claim, _events = _execution_state(exec_id)
    assert execution.status == "succeeded"
    assert [run["status"] for run in _node_runs(exec_id, "notify")] == ["failed"]
    assert execution.context["node"]["recover"]["output"]["recovered"] == "notify"


def test_unknown_mission_outbox_with_missing_mission_blocks_for_reconciliation(tmp_path, monkeypatch):
    _mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    assert workflows_dispatcher.tick(
        limit=1, now=100, state_db_path=state_db_path
    ) == 1
    outbox_id = _node_runs(exec_id, "notify")[0]["outbox_id"]

    # Keep a non-null mission link in the outbox, but make its corresponding
    # workflow aggregate absent to exercise the reconciliation boundary.
    with sqlite3.connect(state_db_path) as conn:
        conn.execute(
            "UPDATE mission_outbox SET mission_id = ? WHERE outbox_id = ?",
            ("mission-missing", outbox_id),
        )

    state_db = SessionDB(db_path=state_db_path)
    try:
        store = MissionOutboxStore(state_db)
        claimed = store.claim(
            now=131,
            owner_id="delivery-test",
            lease_seconds=60,
            limit=1,
        )
        assert store.mark_unknown(
            outbox_id,
            owner_id="delivery-test",
            claim_token=claimed[0].claim_token,
            result={"reason": "router timeout after dispatch"},
        )
    finally:
        state_db.close()

    assert workflows_dispatcher.tick(
        limit=1, now=131, state_db_path=state_db_path
    ) == 0
    execution, _claim, events = _execution_state(exec_id)
    runs = _node_runs(exec_id, "notify")
    assert execution.status == "blocked"
    assert runs[0]["status"] == "blocked"
    assert "unknown_effect" in runs[0]["error"]
    assert "reconciliation" in runs[0]["error"]
    assert [event["kind"] for event in events][-1:] == ["execution_blocked"]


def test_foreign_delivered_outbox_cannot_project_into_active_workflow(tmp_path, monkeypatch):
    _mission_id, exec_id, active_state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    assert workflows_dispatcher.tick(
        limit=1, now=100, state_db_path=active_state_db_path
    ) == 1
    active_outbox_id = _node_runs(exec_id, "notify")[0]["outbox_id"]
    before_events = _execution_state(exec_id)[2]

    foreign_state_db_path = tmp_path / "foreign" / "state.db"
    foreign_state_db = SessionDB(db_path=foreign_state_db_path)
    try:
        store = MissionOutboxStore(foreign_state_db)
        outbox = store.materialize(
            execution_id=exec_id,
            node_id="notify",
            platform="local",
            target="authorized-target",
            content={"text": "ready"},
            not_before=130,
        )
        claimed = store.claim(
            now=131,
            owner_id="foreign-delivery-test",
            lease_seconds=60,
            limit=1,
        )
        assert [row.outbox_id for row in claimed] == [outbox.outbox_id]
        assert store.mark_delivered(
            outbox.outbox_id,
            owner_id="foreign-delivery-test",
            claim_token=claimed[0].claim_token,
            result={"message_id": "foreign-7"},
        )
    finally:
        foreign_state_db.close()

    assert workflows_dispatcher.tick(
        limit=1, now=131, state_db_path=foreign_state_db_path
    ) == 0
    execution, _claim, events = _execution_state(exec_id)
    runs = _node_runs(exec_id, "notify")
    assert execution.status == "waiting"
    assert runs[0]["status"] == "waiting"
    assert runs[0]["outbox_id"] == active_outbox_id
    assert events == before_events


def test_send_message_supports_ordinary_workflow_without_linked_mission(tmp_path, monkeypatch):
    exec_id, state_db_path = _start_unlinked_send_execution(tmp_path, monkeypatch)

    assert workflows_dispatcher.tick(
        limit=1, now=100, state_db_path=state_db_path
    ) == 1

    execution, _claim, _events = _execution_state(exec_id)
    runs = _node_runs(exec_id, "notify")
    assert execution.status == "waiting"
    assert runs[0]["status"] == "waiting"
    assert runs[0]["outbox_id"] is not None


def test_send_message_rejects_cancelled_mission_before_outbox_write(tmp_path, monkeypatch):
    mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    with wfdb.connect() as conn:
        mdb.set_mission_status(conn, mission_id, "cancelled", now=99)

    assert workflows_dispatcher.tick(
        limit=1, now=100, state_db_path=state_db_path
    ) == 1

    execution, _claim, _events = _execution_state(exec_id)
    runs = _node_runs(exec_id, "notify")
    assert execution.status == "failed"
    assert runs[0]["status"] == "failed"
    assert "mission status 'cancelled' does not permit materialization" in runs[0]["error"]
    assert runs[0]["outbox_id"] is None

    state_db = SessionDB(db_path=state_db_path)
    try:
        assert state_db.get_outbox_by_identity(exec_id, "notify") is None
    finally:
        state_db.close()


def test_send_message_rejects_verdicted_mission_before_outbox_write(tmp_path, monkeypatch):
    mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    with wfdb.connect() as conn:
        mdb.set_mission_verdict(conn, mission_id, "failed", now=99)

    assert workflows_dispatcher.tick(
        limit=1, now=100, state_db_path=state_db_path
    ) == 1

    execution, _claim, _events = _execution_state(exec_id)
    runs = _node_runs(exec_id, "notify")
    assert execution.status == "failed"
    assert runs[0]["status"] == "failed"
    assert "mission verdict does not permit materialization" in runs[0]["error"]
    assert runs[0]["outbox_id"] is None

    state_db = SessionDB(db_path=state_db_path)
    try:
        assert state_db.get_outbox_by_identity(exec_id, "notify") is None
    finally:
        state_db.close()


def test_send_message_rejects_revoked_authority_before_outbox_write(tmp_path, monkeypatch):
    mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    with wfdb.connect() as conn:
        authority = mdb.get_mission(conn, mission_id).authority
        authority["revoked"] = True
        conn.execute(
            """
            UPDATE missions
               SET authority_json = ?, authority_version = authority_version + 1, updated_at = ?
             WHERE mission_id = ?
            """,
            (json.dumps(authority), 99, mission_id),
        )

    assert workflows_dispatcher.tick(
        limit=1, now=100, state_db_path=state_db_path
    ) == 1

    execution, _claim, _events = _execution_state(exec_id)
    runs = _node_runs(exec_id, "notify")
    assert execution.status == "failed"
    assert runs[0]["status"] == "failed"
    assert "mission authority is revoked or invalid" in runs[0]["error"]
    assert runs[0]["outbox_id"] is None

    state_db = SessionDB(db_path=state_db_path)
    try:
        assert state_db.get_outbox_by_identity(exec_id, "notify") is None
    finally:
        state_db.close()


def test_send_message_rejects_invalid_authority_before_outbox_write(tmp_path, monkeypatch):
    mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    with wfdb.connect() as conn:
        authority = mdb.get_mission(conn, mission_id).authority
        authority["valid"] = False
        conn.execute(
            """
            UPDATE missions
               SET authority_json = ?, authority_version = authority_version + 1, updated_at = ?
             WHERE mission_id = ?
            """,
            (json.dumps(authority), 99, mission_id),
        )

    assert workflows_dispatcher.tick(
        limit=1, now=100, state_db_path=state_db_path
    ) == 1

    execution, _claim, _events = _execution_state(exec_id)
    runs = _node_runs(exec_id, "notify")
    assert execution.status == "failed"
    assert runs[0]["status"] == "failed"
    assert "mission authority is revoked or invalid" in runs[0]["error"]
    assert runs[0]["outbox_id"] is None

    state_db = SessionDB(db_path=state_db_path)
    try:
        assert state_db.get_outbox_by_identity(exec_id, "notify") is None
    finally:
        state_db.close()


def test_send_message_fails_closed_for_ambiguous_mission_link(tmp_path, monkeypatch):
    _mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    with wfdb.connect() as conn:
        conn.execute(
            """
            INSERT INTO missions (
                mission_id, profile, objective,
                constraints_json, authority_json, evidence_json,
                authority_version, status, verdict, receipt_id,
                created_at, updated_at, terminal_at
            ) VALUES (?, 'default', 'duplicate', '[]', '{}',
                '{"checks":["workflow_succeeded"]}', 1, 'running',
                NULL, NULL, 10, 10, NULL)
            """,
            ("mission_duplicate",),
        )
        conn.execute(
            """
            INSERT INTO mission_execution_links (
                mission_id, execution_id, relation, linked_at
            ) VALUES (?, ?, 'primary', 10)
            """,
            ("mission_duplicate", exec_id),
        )

    assert workflows_dispatcher.tick(
        limit=1, now=100, state_db_path=state_db_path
    ) == 1

    execution, _claim, _events = _execution_state(exec_id)
    runs = _node_runs(exec_id, "notify")
    assert execution.status == "failed"
    assert runs[0]["status"] == "failed"
    assert "mission lookup failed" in runs[0]["error"]


def test_send_message_rejects_target_outside_mission_authority(tmp_path, monkeypatch):
    _mission_id, exec_id, state_db_path = _start_mission_send_execution(
        tmp_path,
        monkeypatch,
        target="unauthorized-target",
        allowed_targets=["authorized-target"],
    )

    assert workflows_dispatcher.tick(
        limit=1, now=100, state_db_path=state_db_path
    ) == 1

    execution, _claim, _events = _execution_state(exec_id)
    runs = _node_runs(exec_id, "notify")
    assert execution.status == "failed"
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert "not authorized" in runs[0]["error"]

    state_db = SessionDB(db_path=state_db_path)
    try:
        assert state_db.get_outbox_by_id(runs[0]["outbox_id"] or "missing") is None
    finally:
        state_db.close()


def test_send_message_rejects_delay_over_configured_maximum(tmp_path, monkeypatch):
    _mission_id, exec_id, state_db_path = _start_mission_send_execution(
        tmp_path, monkeypatch, delay=11
    )
    (state_db_path.parent / "config.yaml").write_text(
        "missions:\n  outbox:\n    max_delay_seconds: 10\n", encoding="utf-8"
    )

    assert workflows_dispatcher.tick(
        limit=1, now=100, state_db_path=state_db_path
    ) == 1

    execution, _claim, _events = _execution_state(exec_id)
    runs = _node_runs(exec_id, "notify")
    assert execution.status == "failed"
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert "configured maximum" in runs[0]["error"]

    state_db = SessionDB(db_path=state_db_path)
    try:
        assert state_db.get_outbox_by_id(runs[0]["outbox_id"] or "missing") is None
    finally:
        state_db.close()


def test_send_message_fails_closed_for_invalid_delay_configuration(tmp_path, monkeypatch):
    _mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    (state_db_path.parent / "config.yaml").write_text(
        "missions:\n  outbox:\n    max_delay_seconds: 0\n", encoding="utf-8"
    )

    assert workflows_dispatcher.tick(
        limit=1, now=100, state_db_path=state_db_path
    ) == 1

    execution, _claim, _events = _execution_state(exec_id)
    runs = _node_runs(exec_id, "notify")
    assert execution.status == "failed"
    assert runs[0]["status"] == "failed"
    assert "max_delay_seconds" in runs[0]["error"]


def test_send_message_requires_delayed_message_mission_effect(tmp_path, monkeypatch):
    _mission_id, exec_id, state_db_path = _start_mission_send_execution(
        tmp_path, monkeypatch, allowed_effects=[]
    )

    assert workflows_dispatcher.tick(
        limit=1, now=100, state_db_path=state_db_path
    ) == 1

    execution, _claim, _events = _execution_state(exec_id)
    runs = _node_runs(exec_id, "notify")
    assert execution.status == "failed"
    assert runs[0]["status"] == "failed"
    assert "does not allow delayed_message" in runs[0]["error"]


def test_send_message_rejects_expired_mission_authority(tmp_path, monkeypatch):
    _mission_id, exec_id, state_db_path = _start_mission_send_execution(
        tmp_path, monkeypatch, expires_at=100
    )

    assert workflows_dispatcher.tick(
        limit=1, now=100, state_db_path=state_db_path
    ) == 1

    execution, _claim, _events = _execution_state(exec_id)
    runs = _node_runs(exec_id, "notify")
    assert execution.status == "failed"
    assert runs[0]["status"] == "failed"
    assert "expired" in runs[0]["error"]


def test_send_message_requires_authority_expiration(tmp_path, monkeypatch):
    _mission_id, exec_id, state_db_path = _start_mission_send_execution(
        tmp_path, monkeypatch, expires_at=None
    )

    assert workflows_dispatcher.tick(
        limit=1, now=100, state_db_path=state_db_path
    ) == 1

    execution, _claim, _events = _execution_state(exec_id)
    runs = _node_runs(exec_id, "notify")
    assert execution.status == "failed"
    assert runs[0]["status"] == "failed"
    assert "expires_at" in runs[0]["error"]


def test_catch_path_wait_resume_does_not_rerun_failed_node(tmp_path, monkeypatch):
    spec = WorkflowSpec.model_validate({
        "id": "catch_wait_demo", "name": "Catch Wait Demo", "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {
            "flaky": {"type": "fail", "output": {"reason": "boom"}, "catch": "pause"},
            "pause": {"type": "wait", "seconds": 5},
            "done": {
                "type": "pass",
                "output": {"after": "${ node.pause.output.waited }", "failed": "${ error.node }"},
            },
        },
        "edges": [{"from": "pause", "to": "done"}],
    })
    exec_id = _start_spec_execution(tmp_path, monkeypatch, spec)

    assert workflows_dispatcher.tick(limit=1, now=100) == 1

    execution, _, _ = _execution_state(exec_id)
    assert execution.status == "waiting"
    assert [run["status"] for run in _node_runs(exec_id, "flaky")] == ["failed"]
    assert [run["status"] for run in _node_runs(exec_id, "pause")] == ["waiting"]

    assert workflows_dispatcher.tick(limit=1, now=105) == 1

    execution, _, _ = _execution_state(exec_id)
    assert execution.status == "succeeded"
    assert [run["status"] for run in _node_runs(exec_id, "flaky")] == ["failed"]
    assert [run["status"] for run in _node_runs(exec_id, "pause")] == ["succeeded"]
    assert execution.context["node"]["done"]["output"] == {"after": True, "failed": "flaky"}


def test_agent_task_creates_kanban_card_and_resumes_after_completion(tmp_path, monkeypatch):
    home = tmp_path / ".hades"
    home.mkdir()
    monkeypatch.setenv("HADES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, _agent_spec(), created_by="test")
        exec_id = wfdb.start_execution(
            conn,
            "agent_demo",
            input_data={"task": "hello", "secret": "leaked"},
            trigger_type="manual",
        )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1

    with wfdb.connect() as conn, kb.connect() as kconn:
        execution = wfdb.get_execution(conn, exec_id)
        tasks = kb.list_tasks(kconn)
        ask = conn.execute(
            """
            SELECT * FROM workflow_node_runs
             WHERE execution_id = ? AND node_id = 'ask'
            """,
            (exec_id,),
        ).fetchall()
    assert execution.status == "waiting"
    assert len(tasks) == 1
    task = tasks[0]
    assert task.workflow_template_id == "agent_demo"
    assert task.current_step_key == "ask"
    assert task.created_by == f"workflow:{exec_id}:version:1:node:ask"
    assert task.assignee == "worker-profile"
    assert task.workspace_path == "workflow-workspace"
    assert task.skills == ["test-driven-development"]
    assert task.max_retries == 2
    assert task.model_override == "test-model"
    assert task.goal_mode is True
    assert task.goal_max_turns == 3
    assert task.status in {"ready", "todo"}
    assert task.body is not None and "hello" in task.body
    assert len(ask) == 1
    assert ask[0]["status"] == "waiting"
    assert ask[0]["kanban_task_id"] == task.id
    assert ask[0]["wait_until"] is None

    assert workflows_dispatcher.tick(limit=1, now=100) == 0
    with kb.connect() as kconn:
        assert len(kb.list_tasks(kconn)) == 1
        assert kb.complete_task(kconn, task.id, result=json.dumps({"answer": "${ input.secret }"}))

    assert workflows_dispatcher.tick(limit=1, now=101) == 1

    with wfdb.connect() as conn:
        execution = wfdb.get_execution(conn, exec_id)
        ask = conn.execute(
            """
            SELECT * FROM workflow_node_runs
             WHERE execution_id = ? AND node_id = 'ask'
            """,
            (exec_id,),
        ).fetchone()
    assert execution.status == "succeeded"
    assert execution.context["node"]["done"]["output"] == {"agent": "${ input.secret }"}
    assert ask["status"] == "succeeded"
    assert json.loads(ask["output_json"]) == {"answer": "${ input.secret }"}


def test_agent_task_passes_provider_and_model_to_kanban_task(tmp_path, monkeypatch):
    spec = WorkflowSpec.model_validate({
        "id": "routed_review",
        "name": "Routed Review",
        "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {
            "review": {
                "type": "agent_task",
                "profile": "reviewer",
                "provider": "openai-codex",
                "model": "gpt-5.5",
                "prompt": "Return JSON only: {\"ok\": true}",
                "result_contract": {"ok": "boolean"},
            }
        },
        "edges": [],
    })
    exec_id = _start_agent_spec_execution(tmp_path, monkeypatch, spec)

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    node_run = _node_runs(exec_id, "review")[0]

    assert node_run["kanban_task_id"]
    with kb.connect() as kconn:
        task = kb.get_task(kconn, node_run["kanban_task_id"])

    assert task is not None
    assert task.assignee == "reviewer"
    assert task.provider_override == "openai-codex"
    assert task.model_override == "gpt-5.5"


def test_agent_task_text_prompt_interpolates_inline_templates(tmp_path, monkeypatch):
    home = tmp_path / ".hades"
    home.mkdir()
    monkeypatch.setenv("HADES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    wfdb.init_db()

    spec = WorkflowSpec.model_validate({
        "id": "text_prompt_demo",
        "name": "Text Prompt Demo",
        "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {
            "prepare": {
                "type": "pass",
                "output": {"repo": "${ input.repo }", "branch": "${ input.branch }"},
            },
            "review": {
                "type": "agent_task",
                "profile": "reviewer",
                "title": "Review branch",
                "prompt": "Review repo ${ node.prepare.output.repo } on branch ${ node.prepare.output.branch }. Return JSON with verdict and reason.",
                "workspace_kind": "scratch",
            },
        },
        "edges": [{"from": "prepare", "to": "review"}],
    })

    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, spec, created_by="test")
        wfdb.start_execution(
            conn,
            spec.id,
            input_data={"repo": "/tmp/app", "branch": "feature/workflow"},
            trigger_type="manual",
        )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1

    with kb.connect() as kconn:
        task = kb.list_tasks(kconn)[0]

    assert task.body is not None
    assert "Security boundary" in task.body
    assert '<workflow_untrusted_value source="node.prepare.output.repo">' in task.body
    assert "/tmp/app" in task.body
    assert '<workflow_untrusted_value source="node.prepare.output.branch">' in task.body
    assert "feature/workflow" in task.body
    assert "${ node.prepare.output.repo }" not in task.body


def test_agent_task_title_interpolates_inline_templates(tmp_path, monkeypatch):
    home = tmp_path / ".hades"
    home.mkdir()
    monkeypatch.setenv("HADES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    wfdb.init_db()

    spec = WorkflowSpec.model_validate({
        "id": "title_prompt_demo",
        "name": "Title Prompt Demo",
        "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {
            "research": {
                "type": "agent_task",
                "profile": "researcher",
                "title": "Research ${ input.topic }",
                "prompt": "Research ${ input.topic } and return JSON only.",
            },
        },
    })

    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, spec, created_by="test")
        wfdb.start_execution(
            conn,
            spec.id,
            input_data={"topic": "ai"},
            trigger_type="manual",
        )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1

    with kb.connect() as kconn:
        task = kb.list_tasks(kconn)[0]

    assert task.title == "Research ai"
    assert "${ input.topic }" not in task.title


def test_agent_task_title_falls_back_to_literal_on_missing_template_path(tmp_path, monkeypatch):
    home = tmp_path / ".hades"
    home.mkdir()
    monkeypatch.setenv("HADES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    wfdb.init_db()

    spec = WorkflowSpec.model_validate({
        "id": "title_fallback_demo",
        "name": "Title Fallback Demo",
        "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {
            "research": {
                "type": "agent_task",
                "profile": "researcher",
                "title": "Research ${ input.missing }",
                "prompt": "Return JSON only.",
            },
        },
    })

    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, spec, created_by="test")
        wfdb.start_execution(conn, spec.id, input_data={}, trigger_type="manual")

    assert workflows_dispatcher.tick(limit=1, now=100) == 1

    with kb.connect() as kconn:
        task = kb.list_tasks(kconn)[0]

    assert task.title == "Research ${ input.missing }"


def test_waiting_on_unresumable_join_fails_execution(tmp_path, monkeypatch):
    home = tmp_path / ".hades"
    home.mkdir()
    monkeypatch.setenv("HADES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    wfdb.init_db()

    spec = WorkflowSpec.model_validate({
        "id": "stuck_join_demo",
        "name": "Stuck Join Demo",
        "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {
            "fork": {"type": "parallel"},
            "merge": {"type": "join"},
        },
        "edges": [],
    })

    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, spec, created_by="test")
        exec_id = wfdb.start_execution(conn, spec.id, input_data={}, trigger_type="manual")
        token = "test-claim-token"
        conn.execute(
            """
            UPDATE workflow_executions
               SET status = 'queued', claim_lock = ?, claim_expires = ?, updated_at = ?
             WHERE execution_id = ?
            """,
            (token, 200, 100, exec_id),
        )
        result = EngineResult(
            status="waiting",
            context={"input": {}, "node": {}, "workflow": {"id": spec.id, "version": 1}},
            waiting_nodes=["merge"],
        )
        assert workflows_dispatcher._finish(
            conn,
            execution_id=exec_id,
            token=token,
            result=result,
            spec=spec,
            now=100,
        )

    with wfdb.connect() as conn:
        execution = wfdb.get_execution(conn, exec_id)
        event = conn.execute(
            """
            SELECT payload_json FROM workflow_events
             WHERE execution_id = ? AND kind = 'execution_failed'
            """,
            (exec_id,),
        ).fetchone()
    assert execution.status == "failed"
    assert event is not None
    payload = json.loads(event["payload_json"])
    assert "unresumable" in payload["error"]["message"].lower()
    assert payload["error"]["waiting_nodes"] == ["merge"]


def test_agent_task_structured_prompt_remains_supported_and_pretty_printed(tmp_path, monkeypatch):
    home = tmp_path / ".hades"
    home.mkdir()
    monkeypatch.setenv("HADES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    wfdb.init_db()

    spec = WorkflowSpec.model_validate({
        "id": "structured_prompt_demo",
        "name": "Structured Prompt Demo",
        "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {
            "ask": {
                "type": "agent_task",
                "profile": "worker",
                "prompt": {
                    "task": "Handle ${ input.topic }",
                    "result_contract": {"summary": "string"},
                },
            },
        },
    })

    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, spec, created_by="test")
        wfdb.start_execution(
            conn,
            spec.id,
            input_data={"topic": "workflow prompts"},
            trigger_type="manual",
        )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    with kb.connect() as kconn:
        body = kb.list_tasks(kconn)[0].body or ""

    assert "workflow_untrusted_value" in body
    assert 'source=\\"input.topic\\"' in body
    assert "workflow prompts" in body
    assert "\n  " in body


def test_cancel_execution_blocks_linked_agent_task(tmp_path, monkeypatch):
    home = tmp_path / ".hades"
    home.mkdir()
    monkeypatch.setenv("HADES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, _agent_spec(), created_by="test")
        exec_id = wfdb.start_execution(
            conn, "agent_demo", input_data={"task": "hello"}, trigger_type="manual"
        )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    task_id = _node_runs(exec_id, "ask")[0]["kanban_task_id"]
    with kb.connect() as kconn:
        task = kb.get_task(kconn, task_id)
        assert task is not None
        assert task.status in {"ready", "todo"}
        kconn.execute(
            """
            UPDATE tasks
               SET status = 'running', claim_lock = 'worker-lock',
                   claim_expires = 999, worker_pid = NULL
             WHERE id = ?
            """,
            (task_id,),
        )

    with wfdb.connect() as conn:
        execution, cancelled = wfdb.cancel_execution(conn, exec_id, source="test")

    assert cancelled is True
    assert execution.status == "cancelled"
    with kb.connect() as kconn:
        task = kb.get_task(kconn, task_id)
        assert task is not None
        event = kconn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        reclaimed_event = kconn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'reclaimed' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    assert task.status == "blocked"
    assert reclaimed_event is not None
    assert event is not None
    assert "cancelled" in json.loads(event["payload"])["reason"]


def test_agent_task_materialization_error_fails_execution(tmp_path, monkeypatch):
    exec_id = _start_agent_spec_execution(tmp_path, monkeypatch, _agent_spec())

    assert workflows_dispatcher.tick(limit=1, now=100) == 1

    execution, claim, events = _execution_state(exec_id)
    ask = _node_runs(exec_id, "ask")[0]
    assert execution.status == "failed"
    assert claim["claim_lock"] is None
    assert ask["status"] == "failed"
    assert "input.task" in ask["error"]
    assert [event["kind"] for event in events][-1] == "execution_failed"
    assert workflows_dispatcher.tick(limit=1, now=101) == 0


def test_agent_task_materialization_error_blocks_sibling_tasks(tmp_path, monkeypatch):
    spec = WorkflowSpec.model_validate({
        "id": "agent_materialization_siblings",
        "name": "Agent Materialization Siblings",
        "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {
            "first": {
                "type": "agent_task",
                "profile": "worker",
                "title": "First task",
                "prompt": "No missing input here",
            },
            "second": {
                "type": "agent_task",
                "profile": "worker",
                "title": "Second task",
                "prompt": "Needs ${ input.missing }",
            },
        },
    })
    exec_id = _start_agent_spec_execution(tmp_path, monkeypatch, spec)

    assert workflows_dispatcher.tick(limit=1, now=100) == 1

    execution, _claim, _events = _execution_state(exec_id)
    assert execution.status == "failed"
    first = _node_runs(exec_id, "first")[0]
    second = _node_runs(exec_id, "second")[0]
    assert first["status"] == "blocked"
    assert first["kanban_task_id"]
    assert second["status"] == "failed"
    with kb.connect() as kconn:
        tasks = kb.list_tasks(kconn)
    assert len(tasks) == 1
    assert tasks[0].id == first["kanban_task_id"]
    assert tasks[0].status == "blocked"


def test_agent_task_resumes_from_summary_only_json_completion(tmp_path, monkeypatch):
    home = tmp_path / ".hades"
    home.mkdir()
    monkeypatch.setenv("HADES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, _agent_spec(), created_by="test")
        exec_id = wfdb.start_execution(
            conn, "agent_demo", input_data={"task": "hello"}, trigger_type="manual"
        )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    with kb.connect() as kconn:
        task = kb.list_tasks(kconn)[0]
        assert kb.complete_task(kconn, task.id, summary=json.dumps({"answer": "from summary"}))

    assert workflows_dispatcher.tick(limit=1, now=101) == 1

    with wfdb.connect() as conn:
        execution = wfdb.get_execution(conn, exec_id)
        ask = conn.execute(
            """
            SELECT * FROM workflow_node_runs
             WHERE execution_id = ? AND node_id = 'ask'
            """,
            (exec_id,),
        ).fetchone()
    assert execution.status == "succeeded"
    assert execution.context["node"]["done"]["output"] == {"agent": "from summary"}
    assert json.loads(ask["output_json"]) == {"answer": "from summary"}


def test_agent_task_resumes_from_summary_only_plain_text_completion(tmp_path, monkeypatch):
    home = tmp_path / ".hades"
    home.mkdir()
    monkeypatch.setenv("HADES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(
            conn,
            _agent_spec(done_output={"agent": "${ node.ask.output.result }"}),
            created_by="test",
        )
        exec_id = wfdb.start_execution(
            conn, "agent_demo", input_data={"task": "hello"}, trigger_type="manual"
        )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    with kb.connect() as kconn:
        task = kb.list_tasks(kconn)[0]
        assert kb.complete_task(kconn, task.id, summary="plain handoff")

    assert workflows_dispatcher.tick(limit=1, now=101) == 1

    with wfdb.connect() as conn:
        execution = wfdb.get_execution(conn, exec_id)
        ask = conn.execute(
            """
            SELECT * FROM workflow_node_runs
             WHERE execution_id = ? AND node_id = 'ask'
            """,
            (exec_id,),
        ).fetchone()
    assert execution.status == "succeeded"
    assert execution.context["node"]["done"]["output"] == {"agent": "plain handoff"}
    assert json.loads(ask["output_json"]) == {"result": "plain handoff"}


def test_agent_task_contract_failure_blocks_sibling_agent_tasks(tmp_path, monkeypatch):
    spec = WorkflowSpec.model_validate({
        "id": "contract_sibling_demo",
        "name": "Contract Sibling Demo",
        "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {
            "first": {
                "type": "agent_task",
                "profile": "worker",
                "prompt": "Return JSON",
                "result_contract": {"status": "ok|failed"},
            },
            "second": {
                "type": "agent_task",
                "profile": "worker",
                "prompt": "Keep working unless workflow stops",
            },
        },
    })
    exec_id = _start_agent_spec_execution(tmp_path, monkeypatch, spec)

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    first = _node_runs(exec_id, "first")[0]
    second = _node_runs(exec_id, "second")[0]
    with kb.connect() as kconn:
        kconn.execute(
            """
            UPDATE tasks
               SET status = 'running', claim_lock = 'worker-lock',
                   claim_expires = 999, worker_pid = NULL
             WHERE id = ?
            """,
            (second["kanban_task_id"],),
        )
        assert kb.complete_task(kconn, first["kanban_task_id"], result=json.dumps({"status": "maybe"}))

    assert workflows_dispatcher.tick(limit=1, now=101) == 0

    first = _node_runs(exec_id, "first")[0]
    second = _node_runs(exec_id, "second")[0]
    with wfdb.connect() as conn:
        execution = wfdb.get_execution(conn, exec_id)
    with kb.connect() as kconn:
        sibling_task = kb.get_task(kconn, second["kanban_task_id"])
        reclaimed_event = kconn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'reclaimed' ORDER BY id DESC LIMIT 1",
            (second["kanban_task_id"],),
        ).fetchone()

    assert execution.status == "blocked"
    assert first["status"] == "blocked"
    assert second["status"] == "blocked"
    assert sibling_task is not None
    assert sibling_task.status == "blocked"
    assert reclaimed_event is not None


def test_agent_task_resumes_from_original_kanban_board_after_current_board_changes(tmp_path, monkeypatch):
    home = tmp_path / ".hades"
    home.mkdir()
    monkeypatch.setenv("HADES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.create_board("workflow-board")
    kb.create_board("other-board")
    kb.set_current_board("workflow-board")
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, _agent_spec(), created_by="test")
        exec_id = wfdb.start_execution(
            conn, "agent_demo", input_data={"task": "hello"}, trigger_type="manual"
        )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    ask = _node_runs(exec_id, "ask")[0]
    task_id = ask["kanban_task_id"]
    assert ask["kanban_board"] == "workflow-board"
    kb.set_current_board("other-board")
    with kb.connect(board="workflow-board") as kconn:
        assert kb.complete_task(kconn, task_id, result=json.dumps({"answer": "from original board"}))

    assert workflows_dispatcher.tick(limit=1, now=101) == 1

    with wfdb.connect() as conn:
        execution = wfdb.get_execution(conn, exec_id)
    ask = _node_runs(exec_id, "ask")[0]
    assert execution.status == "succeeded"
    assert ask["status"] == "succeeded"
    assert execution.context["node"]["done"]["output"] == {"agent": "from original board"}


def test_agent_task_blocks_when_output_missing_required_contract_key(tmp_path, monkeypatch):
    spec = WorkflowSpec.model_validate({
        "id": "contract_agent_demo",
        "name": "Contract Agent Demo",
        "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {
            "ask": {
                "type": "agent_task",
                "profile": "worker",
                "prompt": "Return JSON",
                "result_contract": {"summary": "string", "status": "ok|failed"},
            },
            "done": {"type": "pass", "output": {"ok": True}},
        },
        "edges": [{"from": "ask", "to": "done"}],
    })
    exec_id = _start_agent_spec_execution(tmp_path, monkeypatch, spec)

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    task_id = _node_runs(exec_id, "ask")[0]["kanban_task_id"]
    with kb.connect() as kconn:
        assert kb.complete_task(kconn, task_id, result=json.dumps({"summary": "missing status"}))

    assert workflows_dispatcher.tick(limit=1, now=101) == 0

    with wfdb.connect() as conn:
        execution = wfdb.get_execution(conn, exec_id)
    ask = _node_runs(exec_id, "ask")[0]
    error_text = str(execution.context.get("error"))
    assert execution.status == "blocked"
    assert ask["status"] == "blocked"
    assert "missing required result key: status" in error_text


def test_agent_task_blocks_when_output_contract_type_or_enum_mismatch(tmp_path, monkeypatch):
    spec = WorkflowSpec.model_validate({
        "id": "contract_agent_mismatch_demo",
        "name": "Contract Agent Mismatch Demo",
        "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {
            "ask": {
                "type": "agent_task",
                "profile": "worker",
                "prompt": "Return JSON",
                "result_contract": {
                    "summary": "string",
                    "approved": "boolean",
                    "score": "number",
                    "status": "ok|failed",
                },
            },
            "done": {"type": "pass", "output": {"ok": True}},
        },
        "edges": [{"from": "ask", "to": "done"}],
    })
    exec_id = _start_agent_spec_execution(tmp_path, monkeypatch, spec)

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    task_id = _node_runs(exec_id, "ask")[0]["kanban_task_id"]
    with kb.connect() as kconn:
        assert kb.complete_task(
            kconn,
            task_id,
            result=json.dumps({
                "summary": 123,
                "approved": "yes",
                "score": "high",
                "status": "maybe",
            }),
        )

    assert workflows_dispatcher.tick(limit=1, now=101) == 0

    with wfdb.connect() as conn:
        execution = wfdb.get_execution(conn, exec_id)
    ask = _node_runs(exec_id, "ask")[0]
    error_text = str(execution.context.get("error"))
    assert execution.status == "blocked"
    assert ask["status"] == "blocked"
    assert "result key summary must be string" in error_text
    assert "result key approved must be boolean" in error_text
    assert "result key score must be number" in error_text
    assert "result key status must be one of" in error_text
    assert "failed" in error_text and "ok" in error_text


def test_agent_task_with_matching_result_contract_succeeds(tmp_path, monkeypatch):
    spec = WorkflowSpec.model_validate({
        "id": "contract_agent_success_demo",
        "name": "Contract Agent Success Demo",
        "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {
            "ask": {
                "type": "agent_task",
                "profile": "worker",
                "prompt": "Return JSON",
                "result_contract": {
                    "summary": "string",
                    "status": "ok|failed",
                    "score": "number",
                    "approved": "boolean",
                },
            },
            "done": {"type": "pass", "output": {"ok": True}},
        },
        "edges": [{"from": "ask", "to": "done"}],
    })
    exec_id = _start_agent_spec_execution(tmp_path, monkeypatch, spec)

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    output = {"summary": "done", "status": "ok", "score": 1, "approved": True}
    task_id = _node_runs(exec_id, "ask")[0]["kanban_task_id"]
    with kb.connect() as kconn:
        assert kb.complete_task(kconn, task_id, result=json.dumps(output))

    assert workflows_dispatcher.tick(limit=1, now=101) == 1

    with wfdb.connect() as conn:
        execution = wfdb.get_execution(conn, exec_id)
    ask = _node_runs(exec_id, "ask")[0]
    assert execution.status == "succeeded"
    assert execution.context["node"]["done"]["output"] == {"ok": True}
    assert ask["status"] == "succeeded"
    assert json.loads(ask["output_json"]) == output


def test_blocked_kanban_agent_task_blocks_workflow(tmp_path, monkeypatch):
    home = tmp_path / ".hades"
    home.mkdir()
    monkeypatch.setenv("HADES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, _agent_spec(), created_by="test")
        exec_id = wfdb.start_execution(
            conn, "agent_demo", input_data={"task": "hello"}, trigger_type="manual"
        )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    with kb.connect() as kconn:
        task = kb.list_tasks(kconn)[0]
        assert kb.block_task(kconn, task.id, reason="needs input")

    assert workflows_dispatcher.tick(limit=1, now=101) == 0

    with wfdb.connect() as conn:
        execution = wfdb.get_execution(conn, exec_id)
        ask = conn.execute(
            """
            SELECT * FROM workflow_node_runs
             WHERE execution_id = ? AND node_id = 'ask'
            """,
            (exec_id,),
        ).fetchone()
    assert execution.status == "blocked"
    assert execution.context["error"] == {
        "node_id": "ask",
        "kanban_task_id": task.id,
        "reason": "needs input",
    }
    assert ask["status"] == "blocked"
    assert json.loads(ask["error"]) == execution.context["error"]


def test_auto_blocked_agent_task_uses_last_failure_error_reason(tmp_path, monkeypatch):
    home = tmp_path / ".hades"
    home.mkdir()
    monkeypatch.setenv("HADES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, _agent_spec(), created_by="test")
        exec_id = wfdb.start_execution(
            conn, "agent_demo", input_data={"task": "hello"}, trigger_type="manual"
        )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    with kb.connect() as kconn:
        task = kb.list_tasks(kconn)[0]
        kconn.execute(
            "UPDATE tasks SET status = 'blocked', last_failure_error = ? WHERE id = ?",
            ("spawn failed: missing profile", task.id),
        )

    assert workflows_dispatcher.tick(limit=1, now=101) == 0

    with wfdb.connect() as conn:
        execution = wfdb.get_execution(conn, exec_id)
    assert execution.status == "blocked"
    assert "spawn failed: missing profile" in execution.context["error"]["reason"]


def test_due_schedule_starts_once_and_advances_next_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, _schedule_spec(), created_by="test")
        schedule = dict(conn.execute("SELECT * FROM workflow_schedules").fetchone())

    assert schedule["workflow_id"] == "scheduled_demo"
    assert schedule["version"] == 1
    assert schedule["trigger_id"] == "every_minute"
    assert schedule["schedule"] == "* * * * *"
    old_next_run_at = schedule["next_run_at"]
    assert old_next_run_at is not None

    assert workflows_dispatcher.tick(limit=1, now=old_next_run_at) == 1

    with wfdb.connect() as conn:
        executions = [dict(row) for row in conn.execute(
            """
            SELECT workflow_id, trigger_type, trigger_id, status
              FROM workflow_executions
             WHERE workflow_id = 'scheduled_demo'
            """
        )]
        new_next_run_at = conn.execute(
            "SELECT next_run_at FROM workflow_schedules WHERE id = ?",
            (schedule["id"],),
        ).fetchone()[0]
    assert executions == [{
        "workflow_id": "scheduled_demo",
        "trigger_type": "schedule",
        "trigger_id": "every_minute",
        "status": "succeeded",
    }]
    assert new_next_run_at > old_next_run_at

    assert workflows_dispatcher.tick(limit=1, now=old_next_run_at) == 0
    with wfdb.connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM workflow_executions WHERE workflow_id = 'scheduled_demo'"
        ).fetchone()[0] == 1


def test_due_schedule_uses_trigger_input(tmp_path, monkeypatch):
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    spec = WorkflowSpec.model_validate({
        "id": "scheduled_demo", "name": "Scheduled Demo", "version": 1,
        "triggers": [{
            "type": "schedule",
            "id": "with_input",
            "cron": "* * * * *",
            "input": {"x": 42},
        }],
        "nodes": {"start": {"type": "pass", "output": {"x": "${ input.x }"}}},
    })
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, spec, created_by="test")
        next_run_at = conn.execute("SELECT next_run_at FROM workflow_schedules").fetchone()[0]

    assert workflows_dispatcher.tick(limit=1, now=next_run_at) == 1

    with wfdb.connect() as conn:
        executions = [
            wfdb.get_execution(conn, row["execution_id"])
            for row in conn.execute(
                """
                SELECT execution_id FROM workflow_executions
                 WHERE workflow_id = 'scheduled_demo'
                """
            )
        ]
    assert len(executions) == 1
    execution = executions[0]
    assert execution.input == {"x": 42}
    assert execution.status == "succeeded"
    assert execution.context["node"]["start"]["output"] == {"x": 42}


def test_redeploying_schedule_replaces_older_version_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, _schedule_spec(version=1), created_by="test")
        wfdb.deploy_definition(conn, _schedule_spec(version=2), created_by="test")
        schedules = [dict(row) for row in conn.execute(
            "SELECT version, next_run_at FROM workflow_schedules ORDER BY version"
        )]

    assert [schedule["version"] for schedule in schedules] == [2]

    assert workflows_dispatcher.tick(limit=10, now=schedules[0]["next_run_at"]) == 1

    with wfdb.connect() as conn:
        executions = [dict(row) for row in conn.execute(
            """
            SELECT version, trigger_type, trigger_id
              FROM workflow_executions
             WHERE workflow_id = 'scheduled_demo'
             ORDER BY version
            """
        )]
    assert executions == [{
        "version": 2,
        "trigger_type": "schedule",
        "trigger_id": "every_minute",
    }]


def test_disabling_scheduled_workflow_removes_schedule_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, _schedule_spec(enabled=True), created_by="test")
        due_at = conn.execute("SELECT next_run_at FROM workflow_schedules").fetchone()[0]
        wfdb.deploy_definition(conn, _schedule_spec(version=2, enabled=False), created_by="test")
        schedule_count = conn.execute("SELECT count(*) FROM workflow_schedules").fetchone()[0]

    assert schedule_count == 0
    assert workflows_dispatcher.tick(limit=10, now=due_at) == 0

    with wfdb.connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM workflow_executions WHERE workflow_id = 'scheduled_demo'"
        ).fetchone()[0] == 0


def test_waiting_result_persists_and_is_not_retried(tmp_path, monkeypatch):
    exec_id = _start_spec_execution(tmp_path, monkeypatch, _wait_spec())
    calls = []

    def waiting_result(spec, input_data):
        calls.append((spec.id, input_data))
        return EngineResult(
            status="waiting",
            context={"input": {}, "node": {}},
            waiting_nodes=["pause"],
        )

    monkeypatch.setattr(
        workflows_dispatcher, "run_in_memory_until_waiting", waiting_result
    )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    execution, claim, events = _execution_state(exec_id)
    assert execution.status == "waiting"
    assert execution.context == {"input": {}, "node": {}}
    assert claim == {"claim_lock": None, "claim_expires": None}
    assert [(event["kind"], json.loads(event["payload_json"])) for event in events] == [
        ("execution_started", {}),
        ("execution_waiting", {"waiting_nodes": ["pause"]}),
    ]

    assert workflows_dispatcher.tick(limit=1, now=101) == 0
    assert len(calls) == 1
    assert _execution_state(exec_id)[2] == events


def test_failed_node_retry_schedules_second_attempt(tmp_path, monkeypatch):
    exec_id = _start_spec_execution(
        tmp_path,
        monkeypatch,
        _fail_spec(retry={"max_attempts": 2, "backoff_seconds": 60}),
    )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1

    execution, _, _ = _execution_state(exec_id)
    runs = _node_runs(exec_id, "flaky")
    assert execution.status == "waiting"
    assert execution.context["error"]["node"] == "flaky"
    assert [(run["status"], run["wait_until"]) for run in runs] == [
        ("failed", None),
        ("queued", 160),
    ]
    assert json.loads(runs[0]["error"])["node"] == "flaky"

    assert workflows_dispatcher.tick(limit=1, now=159) == 0
    assert workflows_dispatcher.tick(limit=1, now=160) == 1

    execution, _, _ = _execution_state(exec_id)
    runs = _node_runs(exec_id, "flaky")
    assert execution.status == "failed"
    assert [run["status"] for run in runs] == ["failed", "failed"]


def test_successful_retry_updates_queued_attempt_row(tmp_path, monkeypatch):
    exec_id = _start_spec_execution(
        tmp_path,
        monkeypatch,
        _fail_spec(retry={"max_attempts": 2, "backoff_seconds": 1}),
    )
    calls = []

    def transient_then_success(spec, input_data, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return EngineResult(
                status="failed",
                context={
                    "input": input_data,
                    "workflow": {"id": spec.id, "version": spec.version},
                    "node": {},
                },
                waiting_nodes=[],
                error={"node": "flaky", "type": "transient", "output": {"reason": "boom"}},
            )
        return EngineResult(
            status="succeeded",
            context={
                "input": input_data,
                "workflow": {"id": spec.id, "version": spec.version},
                "node": {"flaky": {"output": {"ok": True}}},
            },
            waiting_nodes=[],
        )

    monkeypatch.setattr(
        workflows_dispatcher, "run_in_memory_until_waiting", transient_then_success
    )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    assert [
        (run["status"], run["wait_until"]) for run in _node_runs(exec_id, "flaky")
    ] == [("failed", None), ("queued", 101)]

    assert workflows_dispatcher.tick(limit=1, now=100) == 0
    assert workflows_dispatcher.tick(limit=1, now=101) == 1

    execution, _, _ = _execution_state(exec_id)
    runs = _node_runs(exec_id, "flaky")
    assert execution.status == "succeeded"
    assert [(run["status"], run["wait_until"]) for run in runs] == [
        ("failed", None),
        ("succeeded", None),
    ]
    assert runs[1]["completed_at"] == 101
    assert json.loads(runs[1]["output_json"]) == {"ok": True}


def test_failed_node_catch_routes_after_max_attempts(tmp_path, monkeypatch):
    exec_id = _start_spec_execution(
        tmp_path,
        monkeypatch,
        _fail_spec(
            retry={"max_attempts": 2, "backoff_seconds": 0},
            catch="recover",
            recover_output={"failed": "${ error.node }", "kind": "${ error.type }"},
        ),
    )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    assert workflows_dispatcher.tick(limit=1, now=100) == 1

    execution, _, _ = _execution_state(exec_id)
    assert execution.status == "succeeded"
    assert execution.context["error"]["node"] == "flaky"
    assert execution.context["node"]["recover"]["output"] == {
        "failed": "flaky",
        "kind": "fail",
    }
    assert [run["status"] for run in _node_runs(exec_id, "flaky")] == [
        "failed",
        "failed",
    ]


def test_catch_route_exception_fails_execution_and_releases_claim(tmp_path, monkeypatch):
    exec_id = _start_spec_execution(
        tmp_path,
        monkeypatch,
        _fail_spec(catch="recover", recover_output={"missing": "${ node.nope.output }"}),
    )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1

    execution, claim, events = _execution_state(exec_id)
    runs = _node_runs(exec_id, "flaky")
    failure_payload = json.loads(events[-1]["payload_json"])

    assert execution.status == "failed"
    assert claim == {"claim_lock": None, "claim_expires": None}
    assert execution.context["error"]["node"] == "flaky"
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert json.loads(runs[0]["error"])["node"] == "flaky"
    assert events[-1]["kind"] == "execution_failed"
    assert "$.node.nope.output" in failure_payload["error"]["message"]
    assert failure_payload["error"]["catch_node"] == "recover"
    assert failure_payload["error"]["caught_node"] == "flaky"


def test_failed_node_without_catch_fails_execution_and_records_attempt(tmp_path, monkeypatch):
    exec_id = _start_spec_execution(tmp_path, monkeypatch, _fail_spec())

    assert workflows_dispatcher.tick(limit=1, now=100) == 1

    execution, _, _ = _execution_state(exec_id)
    runs = _node_runs(exec_id, "flaky")
    assert execution.status == "failed"
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert json.loads(runs[0]["error"]) == execution.context["error"]


def test_failed_node_emits_node_failed_event(tmp_path, monkeypatch):
    exec_id = _start_spec_execution(tmp_path, monkeypatch, _fail_spec())

    assert workflows_dispatcher.tick(limit=1, now=100) == 1

    _execution, _claim, events = _execution_state(exec_id)
    node_failed = [e for e in events if e["kind"] == "node_failed"]
    assert len(node_failed) == 1
    payload = json.loads(node_failed[0]["payload_json"])
    assert payload["node_id"] == "flaky"
    assert payload["error"]["node"] == "flaky"


def test_fire_due_schedules_drops_stale_disabled_schedule(tmp_path, monkeypatch):
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    spec = WorkflowSpec.model_validate({
        "id": "sched_demo", "name": "Sched Demo", "version": 1,
        "triggers": [{"type": "schedule", "id": "daily", "cron": "0 9 * * *"}],
        "nodes": {"start": {"type": "pass", "output": {"ok": True}}},
    })
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, spec, created_by="test")
        # Simulate config drift: definition disabled after the schedule row
        # was registered (e.g. direct DB edit or a crash mid-toggle).
        conn.execute("UPDATE workflow_definitions SET enabled = 0 WHERE workflow_id = 'sched_demo'")
        conn.execute("UPDATE workflow_schedules SET next_run_at = 50 WHERE workflow_id = 'sched_demo'")

    # The stale schedule must not kill the tick — it gets dropped instead.
    assert workflows_dispatcher.tick(limit=5, now=100) == 0

    with wfdb.connect() as conn:
        remaining = conn.execute(
            "SELECT count(*) FROM workflow_schedules WHERE workflow_id = 'sched_demo'"
        ).fetchone()[0]
        executions = conn.execute("SELECT count(*) FROM workflow_executions").fetchone()[0]
    assert remaining == 0
    assert executions == 0


def test_ready_feed_item_for_disabled_definition_is_terminalized(tmp_path, monkeypatch):
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    spec = WorkflowSpec.model_validate({
        "id": "disabled_feed_demo",
        "name": "Disabled Feed Demo",
        "version": 1,
        "triggers": [{"type": "manual", "id": "kickoff", "intake": {"mode": "continuous"}}],
        "nodes": {"start": {"type": "pass"}},
    })
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, spec, created_by="test")
        feed = wfdb.open_input_feed(conn, spec.id, trigger_id="kickoff", now=1)
        item = wfdb.enqueue_input_item(conn, feed.feed_id, {}, now=2)
        conn.execute("UPDATE workflow_definitions SET enabled = 0 WHERE workflow_id = ?", (spec.id,))

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    assert workflows_dispatcher.tick(limit=1, now=101) == 0

    with wfdb.connect() as conn:
        item = wfdb.get_input_item(conn, item.item_id)
        executions = conn.execute("SELECT count(*) FROM workflow_executions").fetchone()[0]
    assert item.status == "failed"
    assert item.execution_id is None
    assert executions == 0


def test_retry_backoff_multiplier_sets_next_wait_until(tmp_path, monkeypatch):
    exec_id = _start_spec_execution(
        tmp_path,
        monkeypatch,
        _fail_spec(retry={"max_attempts": 3, "backoff_seconds": 5, "multiplier": 2}),
    )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    assert [
        (run["status"], run["wait_until"]) for run in _node_runs(exec_id, "flaky")
    ] == [("failed", None), ("queued", 105)]

    assert workflows_dispatcher.tick(limit=1, now=105) == 1
    assert [
        (run["status"], run["wait_until"]) for run in _node_runs(exec_id, "flaky")
    ] == [("failed", None), ("failed", None), ("queued", 115)]


def test_failed_result_persists_deterministic_error_payload(tmp_path, monkeypatch):
    exec_id = _start_execution(tmp_path, monkeypatch)
    monkeypatch.setattr(
        workflows_dispatcher,
        "run_in_memory_until_waiting",
        lambda spec, input_data: EngineResult(
            status="failed",
            context={"input": {}, "node": {}},
            waiting_nodes=[],
            error={"message": "boom"},
        ),
    )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    execution, claim, events = _execution_state(exec_id)
    assert execution.status == "failed"
    assert execution.context == {"input": {}, "node": {}}
    assert claim == {"claim_lock": None, "claim_expires": None}
    assert [(event["kind"], event["payload_json"]) for event in events] == [
        ("execution_started", "{}"),
        ("execution_failed", '{"error":{"message":"boom"}}'),
    ]


def test_engine_exception_persists_failed_and_clears_claim(tmp_path, monkeypatch):
    exec_id = _start_execution(tmp_path, monkeypatch, {"score": 0.9})

    def raise_boom(spec, input_data):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        workflows_dispatcher, "run_in_memory_until_waiting", raise_boom
    )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    execution, claim, events = _execution_state(exec_id)
    assert execution.status == "failed"
    assert execution.context == {"input": {"score": 0.9}, "node": {}}
    assert claim == {"claim_lock": None, "claim_expires": None}
    assert [(event["kind"], json.loads(event["payload_json"])) for event in events] == [
        ("execution_started", {}),
        ("execution_failed", {"error": {"message": "boom"}}),
    ]


def test_non_expired_claim_is_skipped_and_expired_claim_is_reclaimed(tmp_path, monkeypatch):
    exec_id = _start_execution(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        workflows_dispatcher,
        "run_in_memory_until_waiting",
        lambda spec, input_data: calls.append(input_data) or EngineResult(
            status="succeeded",
            context={"input": {}, "node": {}},
            waiting_nodes=[],
        ),
    )

    with wfdb.connect() as conn:
        conn.execute(
            """
            UPDATE workflow_executions
               SET claim_lock = 'busy', claim_expires = 200
             WHERE execution_id = ?
            """,
            (exec_id,),
        )

    assert workflows_dispatcher.tick(limit=1, now=100) == 0
    assert calls == []
    execution, claim, events = _execution_state(exec_id)
    assert execution.status == "queued"
    assert claim == {"claim_lock": "busy", "claim_expires": 200}
    assert events == []

    with wfdb.connect() as conn:
        conn.execute(
            """
            UPDATE workflow_executions
               SET claim_expires = 99
             WHERE execution_id = ?
            """,
            (exec_id,),
        )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    execution, claim, events = _execution_state(exec_id)
    assert execution.status == "succeeded"
    assert claim == {"claim_lock": None, "claim_expires": None}
    assert [event["kind"] for event in events] == [
        "execution_started",
        "execution_succeeded",
    ]
    assert len(calls) == 1


def test_repeated_tick_after_final_status_does_not_duplicate_events(tmp_path, monkeypatch):
    exec_id = _start_execution(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        workflows_dispatcher,
        "run_in_memory_until_waiting",
        lambda spec, input_data: calls.append(input_data) or EngineResult(
            status="succeeded",
            context={"input": {}, "node": {"start": {"output": {"ok": True}}}},
            waiting_nodes=[],
        ),
    )

    assert workflows_dispatcher.tick(limit=1, now=100) == 1
    assert workflows_dispatcher.tick(limit=1, now=101) == 0

    execution, claim, events = _execution_state(exec_id)
    assert execution.status == "succeeded"
    assert claim == {"claim_lock": None, "claim_expires": None}
    assert [event["kind"] for event in events] == [
        "execution_started",
        "node_succeeded",
        "execution_succeeded",
    ]
    assert len(calls) == 1


def test_terminal_outbox_failure_dominates_delivered_sibling_projection(tmp_path, monkeypatch):
    home = tmp_path / ".hades"
    monkeypatch.setenv("HADES_HOME", str(home))
    wfdb.init_db()
    with wfdb.connect() as conn:
        spec = _send_message_spec()
        wfdb.deploy_definition(conn, spec, created_by="test")
        execution_id = wfdb.start_execution(
            conn,
            spec.id,
            input_data={"target": "authorized-target", "body": "ready"},
            trigger_type="manual",
            now=10,
        )
        conn.execute(
            "UPDATE workflow_executions SET status = 'waiting' WHERE execution_id = ?",
            (execution_id,),
        )

    state_db_path = home / "state.db"
    state_db = SessionDB(db_path=state_db_path)
    try:
        store = MissionOutboxStore(state_db)
        delivered = store.materialize(
            execution_id=execution_id,
            node_id="delivered-node",
            platform="local",
            target="authorized-target",
            content="delivered",
        )
        failed = store.materialize(
            execution_id=execution_id,
            node_id="failed-node",
            platform="local",
            target="authorized-target",
            content="failed",
        )
        state_db._execute_write(
            lambda conn: conn.execute(
                "UPDATE mission_outbox SET status = 'delivered' WHERE outbox_id = ?",
                (delivered.outbox_id,),
            )
        )
        state_db._execute_write(
            lambda conn: conn.execute(
                "UPDATE mission_outbox SET status = 'failed' WHERE outbox_id = ?",
                (failed.outbox_id,),
            )
        )
    finally:
        state_db.close()

    with wfdb.connect() as conn:
        conn.execute(
            """
            INSERT INTO workflow_node_runs (execution_id, node_id, status, outbox_id)
            VALUES (?, 'delivered-node', 'waiting', ?), (?, 'failed-node', 'waiting', ?)
            """,
            (execution_id, delivered.outbox_id, execution_id, failed.outbox_id),
        )
        workflows_dispatcher._resume_terminal_outbox_nodes(
            conn,
            now=20,
            state_db_path=state_db_path,
        )
        execution_status = conn.execute(
            "SELECT status FROM workflow_executions WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()["status"]
        node_statuses = [
            row["status"]
            for row in conn.execute(
                "SELECT status FROM workflow_node_runs WHERE execution_id = ? ORDER BY id",
                (execution_id,),
            )
        ]

    assert execution_status == "failed"
    assert node_statuses == ["succeeded", "failed"]


def test_tick_detailed_returns_structured_report(tmp_path, monkeypatch):
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, _switch_spec(), created_by="test")
        wfdb.start_execution(conn, "demo", input_data={"score": 0.9}, trigger_type="manual")

    report = workflows_dispatcher.tick_detailed(limit=1)
    assert report.processed == 1
    assert report.executions_advanced == 1
    assert report.schedules_admitted == 0
    assert report.feed_items_admitted == 0
    assert report.remaining_queued == 0
    assert report.remaining_running_or_waiting == 0


def test_tick_detailed_separates_feed_admission_from_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    spec = WorkflowSpec.model_validate({
        "id": "intake_demo",
        "name": "Intake Demo",
        "version": 1,
        "triggers": [{
            "type": "manual",
            "id": "kickoff",
            "intake": {"mode": "continuous"},
        }],
        "nodes": {"start": {"type": "pass", "output": {"done": True}}},
    })
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, spec, created_by="test")
        feed = wfdb.open_input_feed(conn, spec.id, trigger_id="kickoff")
        wfdb.enqueue_input_item(conn, feed.feed_id, {})

    report = workflows_dispatcher.tick_detailed(limit=2)
    assert report.feed_items_admitted == 1
    assert report.executions_advanced == 1
    assert report.processed == 2


def test_ordinary_send_message_materializes_without_effect_transaction_and_resumes(
    tmp_path, monkeypatch
):
    exec_id, state_db_path = _start_unlinked_send_execution(tmp_path, monkeypatch)

    assert workflows_dispatcher.tick(limit=1, now=100, state_db_path=state_db_path) == 1
    run = _node_runs(exec_id, "notify")[0]
    assert run["status"] == "waiting"
    assert run["outbox_id"]

    state_db = SessionDB(db_path=state_db_path)
    try:
        outbox = state_db.get_outbox_by_id(run["outbox_id"])
        assert outbox is not None
        assert outbox.mission_id is None
        assert outbox.transaction_id is None
        assert outbox.status == "scheduled"
        claimed = MissionOutboxStore(state_db).claim(
            now=131, owner_id="ordinary-test", lease_seconds=60, limit=1
        )
        assert MissionOutboxStore(state_db).mark_delivered(
            outbox.outbox_id,
            owner_id="ordinary-test",
            claim_token=claimed[0].claim_token,
            result={"message_id": "ordinary-1"},
        )
    finally:
        state_db.close()

    assert workflows_dispatcher.tick(limit=1, now=131, state_db_path=state_db_path) == 1
    assert _execution_state(exec_id)[0].status == "succeeded"


def test_missions_outbox_config_caps_delay_without_workflow_namespace(tmp_path, monkeypatch):
    _mission_id, exec_id, state_db_path = _start_mission_send_execution(
        tmp_path, monkeypatch, delay=11
    )
    (state_db_path.parent / "config.yaml").write_text(
        "missions:\n  outbox:\n    max_delay_seconds: 10\n"
        "workflow:\n  outbox:\n    max_delay_seconds: 999999\n",
        encoding="utf-8",
    )

    assert workflows_dispatcher.tick(limit=1, now=100, state_db_path=state_db_path) == 1
    assert _execution_state(exec_id)[0].status == "failed"
    assert "configured maximum" in _node_runs(exec_id, "notify")[0]["error"]


def test_unknown_linked_outbox_blocks_mission_and_reuses_one_review_item(
    tmp_path, monkeypatch
):
    mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    assert workflows_dispatcher.tick(limit=1, now=100, state_db_path=state_db_path) == 1
    outbox_id = _node_runs(exec_id, "notify")[0]["outbox_id"]

    state_db = SessionDB(db_path=state_db_path)
    try:
        store = MissionOutboxStore(state_db)
        claimed = store.claim(now=131, owner_id="delivery-test", lease_seconds=60, limit=1)
        assert store.mark_unknown(
            outbox_id,
            owner_id="delivery-test",
            claim_token=claimed[0].claim_token,
            result={"reason": "router timeout", "token": "secret-token"},
        )
        _set_outbox_effect_phase(state_db, outbox_id, "unknown_effect")
    finally:
        state_db.close()

    assert workflows_dispatcher.tick(limit=1, now=131, state_db_path=state_db_path) == 0
    execution, _claim, events = _execution_state(exec_id)
    run = _node_runs(exec_id, "notify")[0]
    assert execution.status == "blocked"
    assert run["status"] == "blocked"
    assert "execution_failed" not in [event["kind"] for event in events]

    with wfdb.connect() as conn:
        mission = mdb.get_mission(conn, mission_id)
        reviews = conn.execute(
            "SELECT * FROM mission_review_items WHERE mission_id = ?",
            (mission_id,),
        ).fetchall()
    assert mission.status == "blocked"
    assert mission.verdict == "unknown_effect"
    assert len(reviews) == 1
    assert reviews[0]["status"] == "pending"
    assert reviews[0]["kind"] == "unknown_effect"
    assert "secret-token" not in reviews[0]["detail_json"]

    assert workflows_dispatcher.tick(limit=1, now=132, state_db_path=state_db_path) == 0
    with wfdb.connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM mission_review_items WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()[0] == 1


def test_unknown_ordinary_outbox_fails_explicitly_without_mission_review(tmp_path, monkeypatch):
    exec_id, state_db_path = _start_unlinked_send_execution(tmp_path, monkeypatch)
    assert workflows_dispatcher.tick(limit=1, now=100, state_db_path=state_db_path) == 1
    outbox_id = _node_runs(exec_id, "notify")[0]["outbox_id"]

    state_db = SessionDB(db_path=state_db_path)
    try:
        store = MissionOutboxStore(state_db)
        claimed = store.claim(now=131, owner_id="delivery-test", lease_seconds=60, limit=1)
        assert store.mark_unknown(
            outbox_id,
            owner_id="delivery-test",
            claim_token=claimed[0].claim_token,
            result={"reason": "router timeout"},
        )
    finally:
        state_db.close()

    assert workflows_dispatcher.tick(limit=1, now=131, state_db_path=state_db_path) == 0
    execution, _claim, _events = _execution_state(exec_id)
    assert execution.status == "failed"
    assert "ordinary workflow" in _node_runs(exec_id, "notify")[0]["error"]
    with wfdb.connect() as conn:
        assert conn.execute("SELECT count(*) FROM mission_review_items").fetchone()[0] == 0


def test_send_message_rejects_foreign_active_profile_before_materialization(tmp_path, monkeypatch):
    _mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_PROFILE", "foreign")

    assert workflows_dispatcher.tick(limit=1, now=100, state_db_path=state_db_path) == 0
    assert _execution_state(exec_id)[0].status == "queued"
    assert _node_runs(exec_id, "notify") == []
    state_db = SessionDB(db_path=state_db_path)
    try:
        assert state_db.get_outbox_by_identity(exec_id, "notify") is None
    finally:
        state_db.close()


def test_send_message_rejects_foreign_state_db_before_materialization(tmp_path, monkeypatch):
    _mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    foreign_state = tmp_path / "foreign" / "state.db"

    assert workflows_dispatcher.tick(limit=1, now=100, state_db_path=foreign_state) == 0
    assert _execution_state(exec_id)[0].status == "queued"
    assert _node_runs(exec_id, "notify") == []
    assert not foreign_state.exists()
    state_db = SessionDB(db_path=state_db_path)
    try:
        assert state_db.get_outbox_by_identity(exec_id, "notify") is None
    finally:
        state_db.close()


def test_waiting_persistence_failure_cancels_new_outbox_and_is_idempotent(tmp_path, monkeypatch):
    _mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    original = workflows_dispatcher._materialize_send_message

    def materialize_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected node-run persistence failure")

    monkeypatch.setattr(workflows_dispatcher, "_materialize_send_message", materialize_then_fail)
    assert workflows_dispatcher.tick(limit=1, now=100, state_db_path=state_db_path) == 1
    assert _execution_state(exec_id)[0].status == "failed"

    state_db = SessionDB(db_path=state_db_path)
    try:
        outbox = state_db.get_outbox_by_identity(exec_id, "notify")
        assert outbox is not None
        assert outbox.status == "cancelled"
        assert state_db.get_outbox_by_identity(exec_id, "notify").status != "scheduled"
    finally:
        state_db.close()
    assert workflows_dispatcher.tick(limit=1, now=101, state_db_path=state_db_path) == 0


def test_failed_outbox_uses_send_node_retry_semantics(tmp_path, monkeypatch):
    _mission_id, exec_id, state_db_path = _start_mission_send_execution(
        tmp_path,
        monkeypatch,
        retry={"max_attempts": 2, "delay_seconds": 0},
    )
    assert workflows_dispatcher.tick(limit=1, now=100, state_db_path=state_db_path) == 1
    outbox_id = _node_runs(exec_id, "notify")[0]["outbox_id"]
    state_db = SessionDB(db_path=state_db_path)
    try:
        store = MissionOutboxStore(state_db)
        claimed = store.claim(now=131, owner_id="delivery-test", lease_seconds=60, limit=1)
        assert store.mark_failed(
            outbox_id,
            owner_id="delivery-test",
            claim_token=claimed[0].claim_token,
            error="adapter rejected",
        )
        _set_outbox_effect_phase(state_db, outbox_id, "failed")
    finally:
        state_db.close()

    assert workflows_dispatcher.tick(limit=1, now=131, state_db_path=state_db_path) == 1
    execution = _execution_state(exec_id)[0]
    assert execution.status == "queued"
    runs = _node_runs(exec_id, "notify")
    assert [run["status"] for run in runs] == ["failed", "queued"]
    assert "delivery failed" in runs[0]["error"]


def test_cancelled_outbox_uses_send_node_catch_semantics(tmp_path, monkeypatch):
    _mission_id, exec_id, state_db_path = _start_mission_send_execution(
        tmp_path, monkeypatch, catch="recover"
    )
    assert workflows_dispatcher.tick(limit=1, now=100, state_db_path=state_db_path) == 1
    outbox_id = _node_runs(exec_id, "notify")[0]["outbox_id"]
    state_db = SessionDB(db_path=state_db_path)
    try:
        assert MissionOutboxStore(state_db).cancel(outbox_id)
        _set_outbox_effect_phase(state_db, outbox_id, "cancelled")
    finally:
        state_db.close()

    assert workflows_dispatcher.tick(limit=1, now=131, state_db_path=state_db_path) == 1
    execution = _execution_state(exec_id)[0]
    assert execution.status == "succeeded"
    assert execution.context["node"]["recover"]["output"]["recovered"] == "notify"
    assert "delivery cancelled" in execution.context["error"]["message"]


def test_post_materialization_failure_cancels_outbox_before_workflow_retry(
    tmp_path, monkeypatch
):
    _mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    original_append_event = workflows_dispatcher._append_event

    def append_event_then_fail(conn, execution_id, kind, payload, now):
        if kind == "execution_started":
            raise RuntimeError("injected post-materialization failure")
        return original_append_event(conn, execution_id, kind, payload, now)

    monkeypatch.setattr(workflows_dispatcher, "_append_event", append_event_then_fail)
    with pytest.raises(RuntimeError, match="post-materialization"):
        workflows_dispatcher.tick(limit=1, now=100, state_db_path=state_db_path)

    state_db = SessionDB(db_path=state_db_path)
    try:
        outbox = state_db.get_outbox_by_identity(exec_id, "notify")
        assert outbox is not None
        assert outbox.status == "cancelled"
    finally:
        state_db.close()
    assert _node_runs(exec_id, "notify") == []
    assert _execution_state(exec_id)[0].status == "queued"

    monkeypatch.setattr(workflows_dispatcher, "_append_event", original_append_event)
    assert workflows_dispatcher.tick(limit=1, now=101, state_db_path=state_db_path) == 1
    runs = _node_runs(exec_id, "notify")
    assert len(runs) == 1
    assert runs[0]["status"] == "waiting"
    with sqlite3.connect(state_db_path) as conn:
        outbox_count, outbox_status = conn.execute(
            "SELECT count(*), max(status) FROM mission_outbox WHERE execution_id = ?",
            (exec_id,),
        ).fetchone()
    assert outbox_count == 1
    assert outbox_status == "scheduled"


@pytest.mark.parametrize("terminal_status", ["delivered", "unknown"])
def test_wrong_same_profile_mission_is_unknown_effect_without_mutating_wrong_mission(
    tmp_path, monkeypatch, terminal_status
):
    mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    with wfdb.connect() as conn:
        wrong_mission, wrong_execution = mdb.create_mission_and_execution(
            conn,
            workflow_id="send_message_demo",
            objective="wrong mission",
            constraints=[],
            authority={
                "allowed_effects": ["delayed_message"],
                "message_targets": ["local:authorized-target"],
                "expires_at": 1_000,
            },
            evidence={"checks": ["workflow_succeeded"]},
            input_data={"target": "authorized-target", "body": "wrong"},
            profile="default",
            now=11,
        )
        conn.execute(
            "UPDATE workflow_executions SET status = 'succeeded' WHERE execution_id = ?",
            (wrong_execution.execution_id,),
        )
    assert workflows_dispatcher.tick(limit=1, now=100, state_db_path=state_db_path) == 1
    outbox_id = _node_runs(exec_id, "notify")[0]["outbox_id"]
    with sqlite3.connect(state_db_path) as conn:
        conn.execute(
            "UPDATE mission_outbox SET mission_id = ? WHERE outbox_id = ?",
            (wrong_mission.mission_id, outbox_id),
        )
    _terminalize_outbox(state_db_path, outbox_id, terminal_status)

    assert workflows_dispatcher.tick(limit=1, now=131, state_db_path=state_db_path) == 0
    execution, _claim, _events = _execution_state(exec_id)
    run = _node_runs(exec_id, "notify")[0]
    assert execution.status == "blocked"
    assert run["status"] == "blocked"
    assert "unknown_effect" in run["error"]
    assert "mission_id" in run["error"]
    with wfdb.connect() as conn:
        wrong_after = mdb.get_mission(conn, wrong_mission.mission_id)
        expected_after = mdb.get_mission(conn, mission_id)
        wrong_reviews = conn.execute(
            "SELECT count(*) FROM mission_review_items WHERE mission_id = ?",
            (wrong_mission.mission_id,),
        ).fetchone()[0]
    assert wrong_after.status == "running"
    assert wrong_after.verdict is None
    assert wrong_reviews == 0
    assert expected_after.status == "running"
    assert expected_after.verdict is None


@pytest.mark.parametrize("terminal_status", ["delivered", "unknown"])
def test_missing_mission_is_unknown_effect_for_every_terminal_status(
    tmp_path, monkeypatch, terminal_status
):
    _mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    assert workflows_dispatcher.tick(limit=1, now=100, state_db_path=state_db_path) == 1
    outbox_id = _node_runs(exec_id, "notify")[0]["outbox_id"]
    with sqlite3.connect(state_db_path) as conn:
        conn.execute(
            "UPDATE mission_outbox SET mission_id = ? WHERE outbox_id = ?",
            ("mission-missing", outbox_id),
        )
    _terminalize_outbox(state_db_path, outbox_id, terminal_status)

    assert workflows_dispatcher.tick(limit=1, now=131, state_db_path=state_db_path) == 0
    execution, _claim, _events = _execution_state(exec_id)
    run = _node_runs(exec_id, "notify")[0]
    assert execution.status == "blocked"
    assert run["status"] == "blocked"
    assert "unknown_effect" in run["error"]
    assert "reconciliation" in run["error"]


def test_unknown_outbox_preserves_terminal_mission_state(tmp_path, monkeypatch):
    mission_id, exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    assert workflows_dispatcher.tick(limit=1, now=100, state_db_path=state_db_path) == 1
    outbox_id = _node_runs(exec_id, "notify")[0]["outbox_id"]
    with wfdb.connect() as conn:
        mdb.set_mission_status(conn, mission_id, "cancelled", now=120)
    _terminalize_outbox(state_db_path, outbox_id, "unknown")

    assert workflows_dispatcher.tick(limit=1, now=131, state_db_path=state_db_path) == 0
    execution = _execution_state(exec_id)[0]
    run = _node_runs(exec_id, "notify")[0]
    assert execution.status == "blocked"
    assert run["status"] == "blocked"
    assert "unknown_effect" in run["error"]
    with wfdb.connect() as conn:
        mission = mdb.get_mission(conn, mission_id)
        review_count = conn.execute(
            "SELECT count(*) FROM mission_review_items WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()[0]
    assert mission.status == "cancelled"
    assert mission.verdict is None
    assert review_count == 1


def test_mission_authority_rejects_target_allowed_only_on_another_platform(
    tmp_path, monkeypatch
):
    _mission_id, exec_id, state_db_path = _start_mission_send_execution(
        tmp_path,
        monkeypatch,
        allowed_targets=["telegram:authorized-target"],
    )
    assert workflows_dispatcher.tick(limit=1, now=100, state_db_path=state_db_path) == 1
    execution = _execution_state(exec_id)[0]
    run = _node_runs(exec_id, "notify")[0]
    assert execution.status == "failed"
    assert run["status"] == "failed"
    assert "not authorized" in run["error"]


def test_bad_projection_identity_is_blocked_and_unrelated_terminal_row_resumes(
    tmp_path, monkeypatch
):
    _mission_id, first_exec_id, state_db_path = _start_mission_send_execution(tmp_path, monkeypatch)
    with wfdb.connect() as conn:
        second_mission, second_execution = mdb.create_mission_and_execution(
            conn,
            workflow_id="send_message_demo",
            objective="second notification",
            constraints=[],
            authority={
                "allowed_effects": ["delayed_message"],
                "message_targets": ["authorized-target"],
                "expires_at": 1_000,
            },
            evidence={"checks": ["workflow_succeeded"]},
            input_data={"target": "authorized-target", "body": "second"},
            profile="default",
            now=11,
        )
    del second_mission
    assert workflows_dispatcher.tick(limit=2, now=100, state_db_path=state_db_path) == 2
    first_outbox_id = _node_runs(first_exec_id, "notify")[0]["outbox_id"]
    second_outbox_id = _node_runs(second_execution.execution_id, "notify")[0]["outbox_id"]
    with sqlite3.connect(state_db_path) as conn:
        conn.execute(
            "UPDATE mission_outbox SET execution_id = ? WHERE outbox_id = ?",
            ("foreign-execution", first_outbox_id),
        )
    _terminalize_outbox(state_db_path, first_outbox_id, "delivered")
    _terminalize_outbox(state_db_path, second_outbox_id, "delivered")

    assert workflows_dispatcher.tick(limit=1, now=131, state_db_path=state_db_path) == 1
    first_execution = _execution_state(first_exec_id)[0]
    second_execution_after = _execution_state(second_execution.execution_id)[0]
    first_run = _node_runs(first_exec_id, "notify")[0]
    second_run = _node_runs(second_execution.execution_id, "notify")[0]
    assert first_execution.status == "blocked"
    assert first_run["status"] == "blocked"
    assert "identity mismatch" in first_run["error"]
    assert second_execution_after.status == "succeeded"
    assert second_run["status"] == "succeeded"
