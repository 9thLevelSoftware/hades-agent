"""Bounded startup recovery tests (plan Task 5)."""

from __future__ import annotations

import pytest

from agent.effects.recovery import recover_transactions
from tests.agent.effects.effect_harness import (
    AmnesiacAdapter,
    TxHarness,
)


@pytest.fixture()
def harness(tmp_path):
    h = TxHarness(tmp_path)
    try:
        yield h
    finally:
        h.close()


def test_recovery_classifies_landed_inflight_effect(harness):
    harness.create()
    harness.crash_at("after_handler_return")
    harness.restart()
    counts = recover_transactions(
        harness.store, harness.journal, harness.adapters,
    )
    assert counts == {"landed": 1, "not_landed": 0, "unknown": 0, "skipped": 0}
    effect = harness.store.effect_for("tx-1", 1, "workspace_write")
    assert effect.phase == "committed"
    # Recovery only reconciles; it never re-commits.
    assert harness.adapter.commit_calls == 1


def test_recovery_classifies_not_landed_without_retry(harness):
    harness.create()
    harness.crash_at("after_commit_intent")
    harness.restart()
    counts = recover_transactions(
        harness.store, harness.journal, harness.adapters,
    )
    assert counts["not_landed"] == 1
    assert counts["landed"] == 0
    effect = harness.store.effect_for("tx-1", 1, "workspace_write")
    assert effect.phase == "failed"
    # A safe not_landed node resumes only via an explicit later commit;
    # recovery itself never invoked the handler.
    assert harness.adapter.commit_calls == 0


def test_recovery_projects_unknown_review_state_once(tmp_path):
    harness = TxHarness(tmp_path, adapter_cls=AmnesiacAdapter)
    try:
        harness.create()
        harness.crash_at("after_handler_return")
        harness.restart()
        first = recover_transactions(
            harness.store, harness.journal, harness.adapters,
        )
        assert first["unknown"] == 1
        assert harness.store.get_transaction("tx-1").status == "unknown_effect"
        events = [
            e.kind for e in harness.store.load_snapshot("tx-1").events
        ]
        assert events.count("unknown_effect_review") == 1
        # A second pass is idempotent: the unknown was already projected.
        second = recover_transactions(
            harness.store, harness.journal, harness.adapters,
        )
        assert second["unknown"] == 0
        events = [
            e.kind for e in harness.store.load_snapshot("tx-1").events
        ]
        assert events.count("unknown_effect_review") == 1
    finally:
        harness.close()


def test_recovery_never_touches_terminal_transactions(harness):
    harness.create()
    harness.preview("tx-1")
    harness.commit("tx-1")
    before = harness.store.load_snapshot("tx-1")
    counts = recover_transactions(
        harness.store, harness.journal, harness.adapters,
    )
    assert counts == {"landed": 0, "not_landed": 0, "unknown": 0, "skipped": 0}
    after = harness.store.load_snapshot("tx-1")
    assert after.transaction == before.transaction
    assert [e.event_id for e in after.events] == [
        e.event_id for e in before.events
    ]


def test_recovery_respects_limit_and_reports_skips(harness):
    for index in range(3):
        tx_id = f"tx-{index}"
        harness.create(transaction_id=tx_id, node_ids=(f"write-{index}",))
        harness.crash_at("after_commit_intent", transaction_id=tx_id)
    harness.restart()
    counts = recover_transactions(
        harness.store, harness.journal, harness.adapters, limit=2,
    )
    assert counts["not_landed"] == 2
    assert counts["skipped"] == 1


def test_missing_adapter_skips_instead_of_freezing(harness):
    from agent.effects.recovery import reconcile_effect
    from agent.effects.registry import EffectAdapterRegistry

    harness.create()
    harness.crash_at("after_handler_return")
    harness.restart()
    empty_registry = EffectAdapterRegistry()
    effect = harness.store.effect_for("tx-1", 1, "workspace_write")
    disposition = reconcile_effect(
        harness.store, harness.journal, empty_registry, effect,
    )
    # An unregistered adapter must never freeze the effect as a
    # projected unknown — it stays classifiable by a later pass.
    assert disposition == "skipped"
    untouched = harness.store.effect_for("tx-1", 1, "workspace_write")
    assert untouched.phase == "committing"
    assert untouched.reconciliation is None
    # With the adapter present, the same effect classifies normally.
    counts = recover_transactions(
        harness.store, harness.journal, harness.adapters,
    )
    assert counts["landed"] == 1


def test_startup_seam_populates_builtin_adapters(tmp_path, monkeypatch):
    import tools.checkpoint_manager as checkpoint_manager_module

    monkeypatch.setattr(
        checkpoint_manager_module, "CHECKPOINT_BASE",
        tmp_path / "checkpoints",
    )
    monkeypatch.chdir(tmp_path)
    from agent.effects.recovery import recover_transactions_at_startup
    from hades_state import SessionDB

    db = SessionDB(tmp_path / "startup-state.db")
    try:
        counts = recover_transactions_at_startup(db)
        assert counts == {
            "landed": 0, "not_landed": 0, "unknown": 0, "skipped": 0,
        }
    finally:
        db.close()


def test_startup_recovery_honors_configuration(tmp_path, monkeypatch):
    import yaml

    from agent.effects.recovery import recover_transactions_at_startup
    from hades_constants import get_hades_home
    from hades_state import SessionDB

    config_path = get_hades_home() / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump({
        "transactions": {"auto_reconcile_on_start": False},
    }), encoding="utf-8")
    db = SessionDB(tmp_path / "cfg-state.db")
    try:
        counts = recover_transactions_at_startup(db)
        assert counts.get("disabled") is True
        assert counts["landed"] == 0 and counts["unknown"] == 0
    finally:
        db.close()

    # recovery_batch_size bounds the pass when enabled.
    config_path.write_text(yaml.dump({
        "transactions": {
            "auto_reconcile_on_start": True, "recovery_batch_size": 1,
        },
    }), encoding="utf-8")
    from agent.effects.recovery import _startup_recovery_settings

    enabled, batch = _startup_recovery_settings()
    assert enabled is True
    assert batch == 1
