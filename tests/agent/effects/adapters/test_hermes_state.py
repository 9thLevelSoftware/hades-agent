"""Versioned workflow/cron/config adapter tests (plan Task 7).

All three families run against real durable stores in the per-test
profile home: workflows.db, cron jobs.json, and config.yaml. Revision
hashes and owner-module locks are exercised for real, never mocked.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from agent.effects.adapters.hermes_state import (
    HermesConfigAdapter,
    HermesCronAdapter,
    HermesWorkflowAdapter,
)
from agent.effects.coordinator import (
    TransactionCoordinator,
    prepared_from_json,
)
from agent.effects.models import CompensationRequest, EffectContext
from agent.effects.registry import EffectAdapterRegistry
from agent.effects.store import TransactionStore
from agent.operation_journal import OperationJournal
from hades_constants import get_hades_home
from hades_state import SessionDB


class _AllowAll:
    def authorize(self, context, *, consume):
        return SimpleNamespace(
            allowed=True, verdict="allow", code="allow", context_hash="ctx",
        )


def workflow_spec() -> dict:
    return {
        "id": "state_demo",
        "name": "State Demo",
        "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {"start": {"type": "pass"}},
    }


def cron_job() -> dict:
    return {
        "id": "state-cron",
        "name": "State cron",
        "prompt": "durably mutate cron state",
        "schedule": {"kind": "interval", "minutes": 60, "display": "every 60m"},
        "schedule_display": "every 60m",
        "repeat": {"times": None, "completed": 0},
        "enabled": True,
        "state": "scheduled",
        "deliver": "local",
        "skills": [],
    }


class StateHarness:
    def __init__(self, tmp_path, workflow_conn=None):
        self.db = SessionDB(tmp_path / "state.db")
        self.store = TransactionStore(self.db)
        self.journal = OperationJournal(self.db)
        self.adapters = EffectAdapterRegistry()
        self.config_adapter = HermesConfigAdapter()
        self.cron_adapter = HermesCronAdapter()
        self.adapters.register(self.config_adapter)
        self.adapters.register(self.cron_adapter)
        if workflow_conn is not None:
            self.workflow_adapter = HermesWorkflowAdapter(
                conn_factory=lambda: workflow_conn,
            )
            self.adapters.register(self.workflow_adapter)
        self.coordinator = TransactionCoordinator(
            store=self.store,
            adapters=self.adapters,
            journal=self.journal,
            authority_provider_factory=_AllowAll,
        )

    def close(self):
        self.db.close()

    def transact(self, transaction_id, adapter_id, action, args):
        self.store.create_transaction(
            transaction_id=transaction_id, profile="default", title=action,
            authority={"authority_version": 1},
            graph={
                "nodes": [{
                    "node_id": "state", "adapter_id": adapter_id,
                    "action": action, "args": args,
                }],
                "edges": [],
            },
            failure_policy="stop",
        )
        return transaction_id

    def compensate_node(self, adapter, transaction_id):
        effect = self.store.effect_for(transaction_id, 1, "state")
        return adapter.compensate(
            CompensationRequest(
                effect_id=effect.effect_id,
                prepared=prepared_from_json(effect.prepared),
                verified_result_hash="",
            ),
            EffectContext(
                transaction_id=transaction_id, revision=1, node_id="state",
            ),
        )


@pytest.fixture()
def harness(tmp_path):
    h = StateHarness(tmp_path)
    try:
        yield h
    finally:
        h.close()


# ── Config family ───────────────────────────────────────────────────────


def test_config_adapter_detects_before_after_and_restores(harness):
    tx = harness.transact(
        "tx-config", "hermes-config.v1", "set",
        {"key": "display.theme", "value": "night"},
    )
    preview = harness.coordinator.preview(tx)
    assert preview.status == "ready"
    effect = harness.store.effect_for(tx, 1, "state")
    assert effect.preview["before"] != effect.preview["after"]

    assert harness.coordinator.commit(tx).status == "committed"
    config_path = get_hades_home() / "config.yaml"
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert document["display"]["theme"] == "night"

    result = harness.compensate_node(harness.config_adapter, tx)
    assert result.status == "compensated"
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert (document or {}).get("display", {}).get("theme") is None


def test_config_adapter_rejects_secret_keys(harness):
    tx = harness.transact(
        "tx-secret", "hermes-config.v1", "set",
        {"key": "model.api_key", "value": "leak"},
    )
    result = harness.coordinator.preview(tx)
    assert result.status == "blocked"


def test_config_adapter_records_absent_before_state(harness):
    tx = harness.transact(
        "tx-absent", "hermes-config.v1", "set",
        {"key": "display.theme", "value": "day"},
    )
    assert harness.coordinator.preview(tx).status == "ready"
    effect = harness.store.effect_for(tx, 1, "state")
    # Absent-vs-null is preserved: the before state says the key did not
    # exist, not that it was null.
    assert effect.preview["before"].get("exists") is False


def test_config_compensation_blocks_on_concurrent_revision_change(harness):
    tx = harness.transact(
        "tx-conflict", "hermes-config.v1", "set",
        {"key": "display.theme", "value": "night"},
    )
    harness.coordinator.preview(tx)
    assert harness.coordinator.commit(tx).status == "committed"
    # A concurrent out-of-band edit changes the document revision.
    config_path = get_hades_home() / "config.yaml"
    document = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    document["unrelated"] = {"edited": True}
    config_path.write_text(yaml.dump(document), encoding="utf-8")
    result = harness.compensate_node(harness.config_adapter, tx)
    assert result.status == "blocked"


# ── Cron family ─────────────────────────────────────────────────────────


def test_cron_adapter_creates_and_compensation_removes(tmp_path):
    from cron.jobs import get_job, use_cron_store

    with use_cron_store(tmp_path / "profile"):
        harness = StateHarness(tmp_path)
        try:
            tx = harness.transact(
                "tx-cron", "hermes-cron.v1", "create", {"job": cron_job()},
            )
            preview = harness.coordinator.preview(tx)
            assert preview.status == "ready"
            assert harness.coordinator.commit(tx).status == "committed"
            assert get_job("state-cron") is not None

            result = harness.compensate_node(harness.cron_adapter, tx)
            assert result.status == "compensated"
            assert get_job("state-cron") is None
        finally:
            harness.close()


def test_cron_adapter_never_hard_deletes_on_disable(tmp_path):
    from cron.jobs import apply_mutation, get_job, prepare_create, use_cron_store

    with use_cron_store(tmp_path / "profile"):
        apply_mutation(prepare_create(cron_job()))
        assert get_job("state-cron") is not None
        harness = StateHarness(tmp_path)
        try:
            tx = harness.transact(
                "tx-cron-disable", "hermes-cron.v1", "disable",
                {"job_id": "state-cron"},
            )
            assert harness.coordinator.preview(tx).status == "ready"
            assert harness.coordinator.commit(tx).status == "committed"
            job = get_job("state-cron")
            assert job is not None
            assert job.get("enabled") is False

            result = harness.compensate_node(harness.cron_adapter, tx)
            assert result.status == "compensated"
            assert get_job("state-cron").get("enabled") is True
        finally:
            harness.close()


# ── Workflow family ─────────────────────────────────────────────────────


def test_workflow_adapter_deploys_versions_and_compensates(tmp_path):
    from hades_cli import workflows_db as wfdb

    wfdb.init_db()
    with wfdb.connect() as conn:
        harness = StateHarness(tmp_path, workflow_conn=conn)
        try:
            tx = harness.transact(
                "tx-wf", "hermes-workflow.v1", "deploy",
                {"spec": workflow_spec()},
            )
            preview = harness.coordinator.preview(tx)
            assert preview.status == "ready"
            effect = harness.store.effect_for(tx, 1, "state")
            assert effect.preview["before"]["version"] is None
            assert effect.preview["after"]["version"] == 1

            assert harness.coordinator.commit(tx).status == "committed"
            assert wfdb.get_definition_record(conn, "state_demo", 1) is not None

            result = harness.compensate_node(harness.workflow_adapter, tx)
            assert result.status == "compensated"
            # Compensation selects the prior immutable state (disabled);
            # it never edits or deletes the published spec version.
            record = wfdb.get_definition_record(conn, "state_demo", 1)
            assert record is not None
            assert record.enabled is False
        finally:
            harness.close()
