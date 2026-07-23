"""Behavior contracts for the opt-in v23 external-content FTS migration."""

from __future__ import annotations

import sqlite3
import time
from typing import Any

import pytest

from agent.autonomy import (
    ActionContext,
    AuthorityDecision,
    AutonomyRule,
    RuleProvenance,
)
from agent.autonomy.canonical import context_hash
from agent.autonomy.store import ContractDraft, DecisionRecord
from agent.receipt_hashing import canonical_content_hash
from agent.receipt_models import (
    build_claim,
    build_evidence_digest,
    build_observation,
    build_receipt,
    build_requested_outcome,
)
from agent.receipt_store import ReceiptStore
from agent.receipts import ReceiptSourceKey
from gateway.mission_outbox import MissionOutboxStore
from hades_state import SessionDB


_LEGACY_FTS_SQL = """
CREATE VIRTUAL TABLE messages_fts USING fts5(content);
CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(content, tokenize='trigram');

CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;
CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
END;
CREATE TRIGGER messages_fts_update AFTER UPDATE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
    INSERT INTO messages_fts(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;

CREATE TRIGGER messages_fts_trigram_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;
CREATE TRIGGER messages_fts_trigram_delete AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts_trigram WHERE rowid = old.id;
END;
CREATE TRIGGER messages_fts_trigram_update AFTER UPDATE ON messages BEGIN
    DELETE FROM messages_fts_trigram WHERE rowid = old.id;
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;
"""

_DURABLE_TABLES = (
    "receipts",
    "receipt_observations",
    "agent_operations",
    "effect_transactions",
    "mission_outbox",
    "autonomy_contract_versions",
    "autonomy_contract_head",
    "autonomy_runtime_rules",
    "autonomy_rule_events",
    "autonomy_decisions",
    "autonomy_consumptions",
)


def _require_fts(db: SessionDB) -> None:
    if not db._fts_enabled or not db._trigram_available:
        pytest.skip("this SQLite build does not provide the FTS5 trigram tokenizer")


def _normal_sql(sql: str) -> str:
    return "".join(sql.lower().split())


def _sqlite_schema_sql(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = ?",
        (name,),
    ).fetchone()
    assert row is not None and row[0]
    return _normal_sql(row[0])


def _assert_external_content_layout(db: SessionDB) -> None:
    """Distinguish external-content FTS from inline and contentless layouts."""
    base_sql = _sqlite_schema_sql(db._conn, "messages_fts")
    trigram_sql = _sqlite_schema_sql(db._conn, "messages_fts_trigram")
    assert "content='messages'" in base_sql
    assert "content_rowid='id'" in base_sql
    assert "content=''" not in base_sql
    assert "content='messages_fts_trigram_src'" in trigram_sql
    assert "content_rowid='id'" in trigram_sql
    assert tuple(
        row[1] for row in db._conn.execute("PRAGMA table_info(messages_fts)")
    ) == ("content", "tool_name", "tool_calls")
    assert (
        db._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'messages_fts_content'"
        ).fetchone()
        is None
    )
    assert (
        db._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'messages_fts_trigram_content'"
        ).fetchone()
        is None
    )


def _receipt_fixture(*, transaction_id: str):
    evidence = build_evidence_digest(
        evidence_kind="verification_check",
        source_ref="state.db:fts-v23-check",
        producer_id="hades.tests",
        observed_at="2026-07-23T12:00:00Z",
        summary="durable state survived storage maintenance",
        payload_hash=canonical_content_hash({"check": "fts-v23", "result": "pass"}),
    )
    claim = build_claim(
        statement="durable state is preserved",
        evidence_ids=(evidence.evidence_id,),
        verdict="satisfied",
    )
    return build_receipt(
        source=ReceiptSourceKey("mission", "mission-fts-v23"),
        subject_kind="mission",
        subject_id="mission-fts-v23",
        session_id="legacy",
        mission_id="mission-fts-v23",
        transaction_id=transaction_id,
        requested_outcome=build_requested_outcome(
            outcome_kind="state_maintenance",
            description="compact FTS storage without touching durable state",
            producer_id="hades.tests",
        ),
        status="completed_unverified",
        claims=(claim,),
        evidence=(evidence,),
        scorer_id="hades.tests",
        scorer_version="1",
        decided_at="2026-07-23T12:01:00Z",
    )


def _provenance() -> RuleProvenance:
    return RuleProvenance(
        actor_kind="user",
        actor_id="user-fts-v23",
        source_ref="cli",
        observed_at_ms=1_000,
        confirmed_at_ms=2_000,
        confidence_ppm=1_000_000,
    )


def _seed_durable_records(db: SessionDB) -> dict[str, Any]:
    """Use the public stores to seed one linked record per Hades state family."""
    db.create_session("legacy", "test")

    outbox = MissionOutboxStore(db).materialize(
        execution_id="execution-fts-v23",
        node_id="notify",
        platform="test",
        target="ops",
        content={"message": "migration complete"},
        mission_id="mission-fts-v23",
        requires_approval=True,
        not_before=123,
        preview={"summary": "notify after migration"},
    )
    assert outbox.transaction_id is not None
    effect = db.get_effect_transaction(outbox.transaction_id)
    assert effect is not None

    receipt_store = ReceiptStore(db)
    receipt = receipt_store.insert(
        _receipt_fixture(transaction_id=outbox.transaction_id)
    )
    observation = receipt_store.append_observation(
        build_observation(
            receipt_id=receipt.receipt_id,
            status="completed_unverified",
            scorer_id="hades.tests",
            scorer_version="1",
            observed_at="2026-07-23T12:02:00Z",
        )
    )

    stable_rule = AutonomyRule(
        rule_id="stable-message-rule",
        source="user_assertion",
        state="active",
        effect="allow",
        action_classes=("message.send",),
        provenance=_provenance(),
        created_at_ms=1_000,
    )
    autonomy = db.autonomy
    contract = autonomy.materialize_contract(
        ContractDraft(
            profile_id="default",
            compiled_at_ms=10_000,
            rules=(stable_rule,),
            source_fingerprint="config:fts-v23",
        ),
        now_ms=50_000,
    )
    mandate = AutonomyRule(
        rule_id="mandate-fts-v23",
        source="temporary_mandate",
        state="active",
        effect="allow",
        action_classes=("workspace.delete",),
        provenance=_provenance(),
        created_at_ms=1_000,
        max_uses=1,
        remaining_uses=1,
        description="one-use migration canary mandate",
    )
    autonomy.put_runtime_rule(mandate, expected_revision=0, now_ms=50_000)
    context = ActionContext(
        operation_key="operation-fts-v23",
        stage="execute",
        action_class="workspace.delete",
        data_classes=("internal",),
        reversibility="reversible",
        resource_refs=("workspace:/tmp/fts-v23-canary",),
    )
    decision = AuthorityDecision(
        decision_id="decision-fts-v23",
        verdict="allow",
        code="explicit_allow",
        reason="matched temporary mandate",
        authority_version=contract.version,
        authority_hash=contract.content_hash,
        context_hash=context_hash(context),
        matched_rule_ids=("mandate-fts-v23",),
        conflicting_rule_ids=(),
        required_evidence=(),
        clarification=None,
        expires_at_ms=None,
        edit_targets=("autonomy rule edit mandate-fts-v23",),
        budget_reservation=None,
    )
    autonomy.consume_rules_and_record_decision(
        DecisionRecord(
            decision=decision,
            operation_key=context.operation_key,
            stage=context.stage,
            created_at_ms=50_000,
        ),
        ("mandate-fts-v23",),
    )

    return {
        "receipt_id": receipt.receipt_id,
        "receipt_hash": receipt.content_hash,
        "observation_id": observation.observation_id,
        "outbox_id": outbox.outbox_id,
        "outbox_transaction_id": outbox.transaction_id,
        "operation_id": effect.operation_id,
        "contract_version": contract.version,
        "contract_hash": contract.content_hash,
        "rule_events": tuple(
            event.event_type for event in autonomy.list_rule_events("mandate-fts-v23")
        ),
    }


def _durable_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    placeholders = ",".join("?" for _ in _DURABLE_TABLES)
    return {
        "columns": {
            name: tuple(row[1] for row in conn.execute(f"PRAGMA table_info({name})"))
            for name in _DURABLE_TABLES
        },
        "rows": {
            name: tuple(
                tuple(row)
                for row in conn.execute(f"SELECT * FROM {name} ORDER BY rowid")
            )
            for name in _DURABLE_TABLES
        },
        "schema_objects": tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                f"WHERE type IN ('index', 'trigger') AND tbl_name IN ({placeholders}) "
                "ORDER BY type, name",
                _DURABLE_TABLES,
            )
        ),
    }


def _duplicate_row_with_new_primary_key(
    conn: sqlite3.Connection,
    *,
    table: str,
    primary_key: str,
    old_value: str,
    new_value: str,
) -> None:
    columns = tuple(row[1] for row in conn.execute(f"PRAGMA table_info({table})"))
    quoted = ", ".join(f'"{column}"' for column in columns)
    selected = ", ".join(
        "?" if column == primary_key else f'"{column}"' for column in columns
    )
    conn.execute(
        f'INSERT INTO "{table}" ({quoted}) '
        f'SELECT {selected} FROM "{table}" WHERE "{primary_key}" = ?',
        (new_value, old_value),
    )


def _assert_durable_state(
    db: SessionDB,
    *,
    expected: dict[str, Any],
    ids: dict[str, Any],
) -> None:
    assert _durable_snapshot(db._conn) == expected
    assert db._conn.execute("PRAGMA foreign_key_check").fetchall() == []

    receipt_store = ReceiptStore(db)
    receipt = receipt_store.get(ids["receipt_id"])
    assert receipt is not None
    assert receipt.content_hash == ids["receipt_hash"]
    assert receipt.mission_id == "mission-fts-v23"
    assert receipt.transaction_id == ids["outbox_transaction_id"]
    assert tuple(
        observation.observation_id
        for observation in receipt_store.observations(ids["receipt_id"])
    ) == (ids["observation_id"],)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db._conn.execute(
            "UPDATE receipts SET status = 'failed' WHERE receipt_id = ?",
            (ids["receipt_id"],),
        )

    replayed = MissionOutboxStore(db).materialize(
        execution_id="execution-fts-v23",
        node_id="notify",
        platform="test",
        target="ops",
        content={"message": "migration complete"},
        mission_id="mission-fts-v23",
        requires_approval=True,
        not_before=123,
        preview={"summary": "notify after migration"},
    )
    assert replayed.outbox_id == ids["outbox_id"]
    effect = db.get_effect_transaction(ids["outbox_transaction_id"])
    assert effect is not None
    assert effect.operation_id == ids["operation_id"]
    assert effect.mission_id == replayed.mission_id
    assert effect.execution_id == replayed.execution_id
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _duplicate_row_with_new_primary_key(
            db._conn,
            table="effect_transactions",
            primary_key="transaction_id",
            old_value=ids["outbox_transaction_id"],
            new_value="duplicate-effect",
        )

    autonomy = db.autonomy
    assert (
        autonomy.get_contract(ids["contract_version"]).content_hash
        == ids["contract_hash"]
    )
    assert autonomy.get_head().version == ids["contract_version"]
    assert autonomy.get_runtime_rule("mandate-fts-v23").state == "consumed"
    assert (
        tuple(
            event.event_type for event in autonomy.list_rule_events("mandate-fts-v23")
        )
        == ids["rule_events"]
    )
    assert (
        autonomy.get_decision("decision-fts-v23").authority_version
        == ids["contract_version"]
    )
    consumption = db._conn.execute(
        "SELECT rule_id, decision_id FROM autonomy_consumptions "
        "WHERE operation_key = 'operation-fts-v23'"
    ).fetchone()
    assert consumption is not None
    assert tuple(consumption) == ("mandate-fts-v23", "decision-fts-v23")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        db._conn.execute(
            "INSERT INTO autonomy_consumptions "
            "(rule_id, operation_key, stage, decision_id, consumed_at_ms) "
            "VALUES ('missing-rule', 'missing-op', 'execute', 'missing-decision', 1)"
        )


def _build_v22_inline_database(
    path,
    *,
    rows: int = 3,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create a populated v22-shaped store with real Hades durable records."""
    created = SessionDB(db_path=path)
    _require_fts(created)
    ids = _seed_durable_records(created)
    created.close()

    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            DROP TRIGGER IF EXISTS messages_fts_insert;
            DROP TRIGGER IF EXISTS messages_fts_delete;
            DROP TRIGGER IF EXISTS messages_fts_update;
            DROP TRIGGER IF EXISTS messages_fts_trigram_insert;
            DROP TRIGGER IF EXISTS messages_fts_trigram_delete;
            DROP TRIGGER IF EXISTS messages_fts_trigram_update;
            DROP TABLE IF EXISTS messages_fts;
            DROP TABLE IF EXISTS messages_fts_trigram;
            DROP VIEW IF EXISTS messages_fts_trigram_src;
            """
        )
        conn.executescript(_LEGACY_FTS_SQL)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (22)")
        for index in range(rows):
            role = "tool" if index == rows - 1 else "user"
            content = (
                "toolpayloadneedle trigramtoolonly"
                if role == "tool"
                else f"legacy searchable conversation gapneedle{index:04d}"
            )
            conn.execute(
                "INSERT INTO messages(session_id, role, content, tool_name, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "legacy",
                    role,
                    content,
                    "read_file" if role == "tool" else None,
                    time.time() + index,
                ),
            )
        conn.commit()
        return ids, _durable_snapshot(conn)
    finally:
        conn.close()


def test_fresh_store_uses_external_content_and_omits_tool_rows_from_trigram(tmp_path):
    db = SessionDB(db_path=tmp_path / "fresh.db")
    try:
        _require_fts(db)
        db.create_session("fresh", "test")
        db.append_message("fresh", "user", "freshconversationneedle")
        db.append_message("fresh", "assistant", "assistantconversationneedle")
        db.append_message(
            "fresh",
            "tool",
            "toolpayloadneedle trigramtoolonly",
            tool_name="read_file",
        )

        _assert_external_content_layout(db)
        assert (
            db._conn.execute("SELECT version FROM schema_version").fetchone()[0] == 23
        )
        assert db.get_meta("fts_storage_version") == "1"
        assert {
            row[0]
            for row in db._conn.execute("SELECT id FROM messages_fts_trigram_src")
        } == {
            row[0]
            for row in db._conn.execute("SELECT id FROM messages WHERE role <> 'tool'")
        }
        assert len(db.search_messages("toolpayloadneedle", role_filter=["tool"])) == 1
        assert (
            db._conn.execute(
                "SELECT COUNT(*) FROM messages_fts_trigram "
                "WHERE messages_fts_trigram MATCH 'trigramtoolonly'"
            ).fetchone()[0]
            == 0
        )
    finally:
        db.close()


def test_v23_cjk_search_works_without_the_later_native_extension(tmp_path):
    """The existing trigram and tool-role LIKE behavior remains sufficient."""
    db = SessionDB(db_path=tmp_path / "cjk.db")
    try:
        _require_fts(db)
        db.create_session("cjk", "test")
        conversation_id = db.append_message("cjk", "user", "搜索大别山项目相关资料")
        tool_id = db.append_message(
            "cjk",
            "tool",
            "错误日志：数据库连接超时",
            tool_name="terminal",
        )

        assert [hit["id"] for hit in db.search_messages("大别山项目")] == [
            conversation_id
        ]
        assert [
            hit["id"] for hit in db.search_messages("数据库连接", role_filter=["tool"])
        ] == [tool_id]
    finally:
        db.close()


def test_existing_inline_fts_opens_without_conversion_but_schema_advances(tmp_path):
    path = tmp_path / "legacy.db"
    ids, durable = _build_v22_inline_database(path)

    db = SessionDB(db_path=path)
    try:
        _require_fts(db)
        assert "content='messages'" not in _sqlite_schema_sql(db._conn, "messages_fts")
        assert (
            db._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'messages_fts_content'"
            ).fetchone()
            is not None
        )
        assert db.fts_rebuild_status() is None
        assert (
            db._conn.execute("SELECT version FROM schema_version").fetchone()[0] == 23
        )
        assert db.get_meta("fts_storage_version") is None
        assert db.get_meta("fts_optimize_available") == "1"
        assert db.fts_optimize_available() is True
        assert len(db.search_messages("toolpayloadneedle", role_filter=["tool"])) == 1
        db.append_message("legacy", "user", "legacywriteafteropen")
        assert len(db.search_messages("legacywriteafteropen")) == 1
        _assert_durable_state(db, expected=durable, ids=ids)
    finally:
        db.close()


class _InjectedInterruption(RuntimeError):
    pass


def test_optimize_storage_resumes_after_a_public_step_and_preserves_hades_state(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "legacy.db"
    ids, durable = _build_v22_inline_database(path, rows=2_000)
    db = SessionDB(db_path=path)
    try:
        _require_fts(db)
        real_step = db.fts_rebuild_step

        def interrupt_after_observed_partial_step():
            more = real_step()
            progress = db.get_meta("fts_rebuild_progress")
            high_water = db.get_meta("fts_rebuild_high_water")
            if (
                more
                and progress is not None
                and high_water is not None
                and 0 < int(progress) < int(high_water)
            ):
                raise _InjectedInterruption("simulated stop after a durable chunk")
            return more

        monkeypatch.setattr(
            db, "fts_rebuild_step", interrupt_after_observed_partial_step
        )
        with pytest.raises(_InjectedInterruption):
            db.optimize_fts_storage(vacuum=False)

        progress = int(db.get_meta("fts_rebuild_progress") or 0)
        high_water = int(db.get_meta("fts_rebuild_high_water") or 0)
        assert 0 < progress < high_water
        status = db.fts_rebuild_status()
        assert status is not None
        assert status["indexed"] == progress
        assert status["total"] == high_water
        assert db.get_meta("fts_storage_version") is None

        gap_row = db._conn.execute(
            "SELECT id, content FROM messages "
            "WHERE id > ? AND id <= ? ORDER BY id LIMIT 1",
            (progress, high_water),
        ).fetchone()
        assert gap_row is not None
        gap_id, gap_content = gap_row
        gap_token = gap_content.split()[-1]
        assert [hit["id"] for hit in db.search_messages(gap_token)] == [gap_id]

        post_high_water_id = db.append_message("legacy", "user", "posthighwaterneedle")
        assert post_high_water_id > high_water
        assert (
            db._conn.execute(
                "SELECT rowid FROM messages_fts "
                "WHERE rowid = ? AND messages_fts MATCH 'posthighwaterneedle'",
                (post_high_water_id,),
            ).fetchone()[0]
            == post_high_water_id
        )
        assert [hit["id"] for hit in db.search_messages("posthighwaterneedle")] == [
            post_high_water_id
        ]
        _assert_durable_state(db, expected=durable, ids=ids)
    finally:
        db.close()

    reopened = SessionDB(db_path=path)
    try:
        _require_fts(reopened)
        assert reopened.fts_optimize_available() is True
        assert reopened.get_meta("fts_storage_version") is None
        assert [hit["id"] for hit in reopened.search_messages(gap_token)] == [gap_id]
        _assert_durable_state(reopened, expected=durable, ids=ids)

        result = reopened.optimize_fts_storage(vacuum=False)

        assert result["ok"] is True
        assert reopened.fts_rebuild_status() is None
        assert reopened.get_meta("fts_storage_version") == "1"
        assert reopened.get_meta("fts_rebuild_high_water") is None
        assert reopened.get_meta("fts_rebuild_progress") is None
        assert reopened.fts_optimize_available() is False
        _assert_external_content_layout(reopened)
        assert [hit["id"] for hit in reopened.search_messages(gap_token)] == [gap_id]
        assert [
            hit["id"] for hit in reopened.search_messages("posthighwaterneedle")
        ] == [post_high_water_id]
        _assert_durable_state(reopened, expected=durable, ids=ids)
    finally:
        reopened.close()
