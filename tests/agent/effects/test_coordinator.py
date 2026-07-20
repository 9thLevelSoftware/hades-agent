"""Coordinator state-machine, crash, and middleware tests (plan Task 5)."""

from __future__ import annotations

import pytest

from agent.effects.context import (
    TransactionExecutionContext,
    transaction_context,
    transaction_context_from_runtime,
)
from agent.effects.models import EffectBlocked
from tests.agent.effects.effect_harness import (
    FAULT_POINTS,
    AmnesiacAdapter,
    DenyAllProvider,
    TxHarness,
)


@pytest.fixture()
def harness(tmp_path):
    h = TxHarness(tmp_path)
    try:
        yield h
    finally:
        h.close()


# ── Preview ─────────────────────────────────────────────────────────────


def test_preview_prepares_all_nodes_and_reaches_ready(harness):
    harness.create(node_ids=("write-a", "write-b"),
                   edges=(("write-a", "write-b"),))
    result = harness.preview("tx-1")
    assert result.status == "ready"
    assert result.preview_hash
    assert harness.store.get_transaction("tx-1").status == "ready"
    for node_id in ("write-a", "write-b"):
        effect = harness.store.effect_for("tx-1", 1, node_id)
        assert effect.phase == "previewed"
        assert effect.preview_hash == result.preview_hash
    revision = harness.store.get_revision("tx-1", 1)
    assert revision.preview_hash == result.preview_hash
    # Prepare/preview perform no outward effect.
    assert harness.adapter.commit_calls == 0


def test_preview_failure_blocks_without_committing(harness):
    harness.create()
    # Unregistered adapter id in the graph → preview must block.
    harness.store.create_transaction(
        transaction_id="tx-bad", profile="default", title="bad",
        authority={"authority_version": 1},
        graph={
            "nodes": [{
                "node_id": "ghost", "adapter_id": "missing.v1",
                "action": "write", "args": {"path": "g", "content": "x"},
            }],
            "edges": [],
        },
        failure_policy="stop",
    )
    result = harness.coordinator.preview("tx-bad")
    assert result.status == "blocked"
    assert harness.store.get_transaction("tx-bad").status == "blocked"
    events = [e.kind for e in harness.store.load_snapshot("tx-bad").events]
    assert "preview_failed" in events


# ── Commit ordering ─────────────────────────────────────────────────────


def test_commit_orders_durable_intent_before_handler_and_result_before_success(harness):
    harness.create()
    harness.preview("tx-1")
    harness.trace.clear()
    result = harness.coordinator.commit("tx-1")
    assert result.status == "committed"
    assert harness.trace == [
        "authority_rechecked", "journal_running", "effect_committing",
        "handler_called", "raw_result_persisted", "verified",
        "journal_confirmed", "receipt_persisted",
    ]
    assert (harness.workspace / "workspace_write.txt").read_text(
        encoding="utf-8"
    ) == "content of workspace_write\n"


def test_commit_requires_ready_and_verified_parents(harness):
    harness.create(node_ids=("write-a", "write-b"),
                   edges=(("write-a", "write-b"),))
    with pytest.raises(EffectBlocked, match="ready"):
        harness.coordinator.commit("tx-1")
    harness.preview("tx-1")
    partial = harness.coordinator.commit("tx-1", through_node="write-a")
    assert partial.status == "ready"
    assert harness.store.effect_for("tx-1", 1, "write-a").phase == "verified"
    assert harness.store.effect_for("tx-1", 1, "write-b").phase == "previewed"
    final = harness.coordinator.commit("tx-1")
    assert final.status == "committed"


def test_stale_authority_blocks_commit_with_zero_handler_calls(harness):
    harness.create()
    harness.preview("tx-1")
    harness.provider = DenyAllProvider()
    result = harness.coordinator.commit("tx-1")
    assert result.status == "blocked"
    assert harness.adapter.commit_calls == 0
    assert harness.store.get_transaction("tx-1").status == "blocked"
    events = [e.kind for e in harness.store.load_snapshot("tx-1").events]
    assert "authority_blocked" in events


# ── Crash and recovery ──────────────────────────────────────────────────


@pytest.mark.parametrize("fault", list(FAULT_POINTS))
def test_restart_never_blind_retries_ambiguous_effect(harness, fault):
    harness.create()
    harness.crash_at(fault)
    harness.restart()
    recovered = harness.coordinator.reconcile("tx-1")
    assert harness.adapter.commit_calls <= 1
    if fault in {"after_handler_return", "after_delivery_dispatch"}:
        assert recovered.status in {"committed", "unknown_effect"}


def test_landed_crash_recovers_evidence_without_second_commit(harness):
    harness.create()
    harness.crash_at("after_handler_return")
    assert harness.adapter.commit_calls == 1
    harness.restart()
    recovered = harness.coordinator.reconcile("tx-1")
    assert recovered.status == "committed"
    assert recovered.counts["landed"] == 1
    assert harness.adapter.commit_calls == 1
    effect = harness.store.effect_for("tx-1", 1, "workspace_write")
    assert effect.phase == "committed"
    assert effect.reconciliation["disposition"] == "landed"


def test_unreconcilable_crash_is_unknown_and_frozen(tmp_path):
    harness = TxHarness(tmp_path, adapter_cls=AmnesiacAdapter)
    try:
        harness.create()
        harness.crash_at("after_handler_return")
        harness.restart()
        recovered = harness.coordinator.reconcile("tx-1")
        assert recovered.status == "unknown_effect"
        effect = harness.store.effect_for("tx-1", 1, "workspace_write")
        assert effect.phase == "unknown_effect"
        # A second commit never blind-retries the unknown node.
        with pytest.raises(EffectBlocked):
            harness.coordinator.commit("tx-1")
        assert harness.adapter.commit_calls == 1
    finally:
        harness.close()


# ── Execution context and middleware integration ────────────────────────


def test_transaction_context_is_scoped_and_restored():
    assert transaction_context_from_runtime() is None
    with transaction_context("tx-1", 1, "node-a", coordinator="sentinel"):
        ctx = transaction_context_from_runtime()
        assert ctx.transaction_id == "tx-1"
        assert ctx.node_id == "node-a"
        with transaction_context("tx-2", 3, "node-b"):
            assert transaction_context_from_runtime().transaction_id == "tx-2"
        assert transaction_context_from_runtime().transaction_id == "tx-1"
    assert transaction_context_from_runtime() is None


def test_subprocess_correlation_requires_all_three_env_vars(monkeypatch):
    monkeypatch.setenv("HERMES_TRANSACTION_ID", "tx-9")
    monkeypatch.setenv("HERMES_TRANSACTION_REVISION", "2")
    monkeypatch.delenv("HERMES_TRANSACTION_NODE_ID", raising=False)
    assert transaction_context_from_runtime() is None
    monkeypatch.setenv("HERMES_TRANSACTION_NODE_ID", "send")
    ctx = transaction_context_from_runtime()
    # All three set → correlation accepted, but with no registered
    # runtime coordinator it still fails closed to pass-through.
    assert ctx is None or ctx.coordinator is not None


def test_context_without_node_fails_closed():
    with transaction_context("tx-1", 1, ""):
        assert transaction_context_from_runtime() is None


def test_middleware_passes_through_untouched_without_context(monkeypatch):
    from hades_cli.middleware import run_tool_execution_middleware

    monkeypatch.setattr(
        "agent.autonomy.runtime.authority_gate",
        lambda tool, args, call, **kw: call(args),
    )
    sentinel = {"echo": True}
    calls = []

    def next_call(payload):
        calls.append(payload)
        return sentinel

    result = run_tool_execution_middleware("write_file", {"path": "x"}, next_call)
    assert result is sentinel
    assert calls == [{"path": "x"}]


def test_middleware_routes_in_transaction_calls_to_coordinator(
    harness, monkeypatch
):
    from hades_cli.middleware import run_tool_execution_middleware

    monkeypatch.setattr(
        "agent.autonomy.runtime.authority_gate",
        lambda tool, args, call, **kw: call(args),
    )
    harness.create()
    harness.preview("tx-1")
    captured = {}

    class _RecordingCoordinator:
        def commit_tool_effect(self, **kwargs):
            captured.update(kwargs)
            return {"routed": True}

    keys = []

    def key_factory(effective=None):
        keys.append(effective)
        return "op-key"

    with transaction_context(
        "tx-1", 1, "workspace_write", coordinator=_RecordingCoordinator()
    ):
        result = run_tool_execution_middleware(
            "write_file",
            {"path": "workspace_write.txt", "content": "plugin-final\n"},
            lambda payload: {"handler": True},
            operation_key_factory=key_factory,
        )
    assert result == {"routed": True}
    assert captured["tool_name"] == "write_file"
    assert captured["effective_args"]["content"] == "plugin-final\n"
    # Operation key is computed from the final effective arguments at the
    # terminal boundary, not from pre-plugin arguments.
    assert captured["operation_key"] == "op-key"
    assert keys[-1] == {"path": "workspace_write.txt", "content": "plugin-final\n"}


def test_commit_tool_effect_fails_closed_on_unplanned_node(harness):
    harness.create()
    harness.preview("tx-1")
    execution = TransactionExecutionContext(
        transaction_id="tx-1", revision=1, node_id="not-in-graph",
        coordinator=harness.coordinator,
    )
    with pytest.raises(EffectBlocked, match="planned"):
        harness.coordinator.commit_tool_effect(
            tool_name="write_file", effective_args={"path": "x"},
            operation_key="k", invoke=lambda payload: {"ok": True},
            execution=execution,
        )
    assert harness.adapter.commit_calls == 0
