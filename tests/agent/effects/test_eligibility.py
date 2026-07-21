"""Truthful eligibility and cascade compensation tests (plan Task 9)."""

from __future__ import annotations

import dataclasses

import pytest

from agent.effects.eligibility import (
    eligibility_for_effect,
    plan_compensation,
)
from agent.effects.models import EffectBlocked
from tests.agent.effects.effect_harness import (
    AllowAllProvider,
    DenyAllProvider,
    FileBackedAdapter,
    TxHarness,
)


class SemanticAdapter(FileBackedAdapter):
    descriptor = dataclasses.replace(
        FileBackedAdapter.descriptor, adapter_id="semantic.v1",
        compensation="semantic",
    )


class RigidAdapter(FileBackedAdapter):
    """Declares no compensation at all: undo is simply unsupported."""

    descriptor = dataclasses.replace(
        FileBackedAdapter.descriptor, adapter_id="rigid.v1",
        compensation="none", irreversible_after="commit",
    )

    def compensate(self, request, context):  # pragma: no cover — never called
        raise AssertionError("rigid adapter must never be asked to compensate")


class WindowedAdapter(FileBackedAdapter):
    descriptor = dataclasses.replace(
        FileBackedAdapter.descriptor, adapter_id="windowed.v1",
        compensation_window_seconds=1,
    )


class EligibilityHarness(TxHarness):
    """TxHarness plus extra adapter families and a committed helper."""

    def _build(self):
        super()._build()
        for extra in (SemanticAdapter, RigidAdapter, WindowedAdapter):
            self.adapters.register(extra(self.workspace))

    def graph_for(self, specs, edges=()):
        return {
            "nodes": [
                {
                    "node_id": node_id,
                    "adapter_id": adapter_id,
                    "action": "write",
                    "args": {
                        "path": f"{node_id}.txt",
                        "content": f"content of {node_id}\n",
                    },
                }
                for node_id, adapter_id in specs
            ],
            "edges": [
                {"parent": parent, "child": child} for parent, child in edges
            ],
        }

    def commit_graph(self, specs, edges=(), transaction_id="tx-1"):
        self.store.create_transaction(
            transaction_id=transaction_id, profile="default", title="t",
            authority={"authority_version": 1},
            graph=self.graph_for(specs, edges), failure_policy="stop",
        )
        assert self.coordinator.preview(transaction_id).status == "ready"
        assert self.coordinator.commit(transaction_id).status == "committed"

    def eligibility(self, node_id, *, cascade=False, transaction_id="tx-1",
                    clock=None):
        return eligibility_for_effect(
            self.store, self.adapters, transaction_id, node_id,
            cascade=cascade,
            authority_provider_factory=lambda: self.provider,
            clock=clock,
        )


@pytest.fixture()
def harness(tmp_path):
    h = EligibilityHarness(tmp_path)
    try:
        yield h
    finally:
        h.close()


# ── Eligibility matrix ──────────────────────────────────────────────────


def test_exact_clean_is_eligible(harness):
    harness.commit_graph([("write", "faketest.v1")])
    result = harness.eligibility("write")
    assert result.code == "eligible_exact"
    assert result.can_execute
    assert result.fidelity == "exact"


def test_semantic_clean_is_eligible_compensation(harness):
    harness.commit_graph([("write", "semantic.v1")])
    result = harness.eligibility("write")
    assert result.code == "eligible_compensation"
    assert result.can_execute
    assert result.fidelity == "semantic"


def test_live_dependent_blocks_without_cascade(harness):
    harness.commit_graph(
        [("parent", "faketest.v1"), ("child", "faketest.v1")],
        edges=(("parent", "child"),),
    )
    result = harness.eligibility("parent")
    assert result.code == "blocked_live_dependents"
    assert not result.can_execute
    cascaded = harness.eligibility("parent", cascade=True)
    assert cascaded.code == "eligible_exact"
    assert cascaded.required_cascade_node_ids == ("child", "parent")


def test_irreversible_descendant_blocks_cascade(harness):
    harness.commit_graph(
        [("parent", "faketest.v1"), ("child", "rigid.v1")],
        edges=(("parent", "child"),),
    )
    result = harness.eligibility("parent", cascade=True)
    assert result.code == "blocked_irreversible_boundary"
    assert not result.can_execute


def test_unknown_descendant_blocks(harness):
    harness.commit_graph(
        [("parent", "faketest.v1"), ("child", "faketest.v1")],
        edges=(("parent", "child"),),
    )
    effect = harness.store.effect_for("tx-1", 1, "child")
    assert harness.store.transition_effect(
        effect.effect_id, {"verified"}, "unknown_effect",
    )
    result = harness.eligibility("parent", cascade=True)
    assert result.code == "blocked_unknown"


def test_resource_drift_blocks_exact_undo(harness):
    harness.commit_graph([("write", "faketest.v1")])
    (harness.workspace / "write.txt").write_text(
        "human edit\n", encoding="utf-8",
    )
    result = harness.eligibility("write")
    assert result.code == "blocked_drift"


def test_window_expired_blocks(harness):
    harness.commit_graph([("write", "windowed.v1")])
    late_clock = lambda: __import__("time").time() * 1000 + 3_600_000
    result = harness.eligibility("write", clock=late_clock)
    assert result.code == "blocked_window_expired"


def test_authority_revoked_blocks(harness):
    harness.commit_graph([("write", "faketest.v1")])
    harness.provider = DenyAllProvider()
    result = harness.eligibility("write")
    assert result.code == "blocked_authority"


def test_no_compensate_is_unsupported(harness):
    harness.commit_graph([("write", "rigid.v1")])
    result = harness.eligibility("write")
    assert result.code == "unsupported"


def test_already_compensated_is_terminal(harness):
    harness.commit_graph([("write", "faketest.v1")])
    outcome = harness.coordinator.compensate("tx-1", "write")
    assert outcome.status == "compensated"
    result = harness.eligibility("write")
    assert result.code == "already_compensated"
    assert not result.can_execute


def test_every_result_carries_a_human_reason(harness):
    harness.commit_graph([("write", "rigid.v1")])
    result = harness.eligibility("write")
    assert result.reason
    assert result.code in result.reason or "compensation" in result.reason


# ── Cascade planning and execution ──────────────────────────────────────


def _chain_harness(harness):
    harness.commit_graph(
        [("a", "faketest.v1"), ("b", "faketest.v1"),
         ("c", "faketest.v1"), ("d", "faketest.v1")],
        edges=(("a", "b"), ("b", "c"), ("c", "d")),
    )


def test_cascade_compensates_reverse_topological_order_once(harness):
    _chain_harness(harness)
    plan = plan_compensation(harness.store, "tx-1", "a", cascade=True)
    assert plan.node_ids == ("d", "c", "b", "a")
    assert plan.plan_hash

    first = harness.coordinator.compensate("tx-1", "a", cascade=True)
    assert first.status == "compensated"
    assert first.compensated_nodes == ("d", "c", "b", "a")
    for node_id in ("a", "b", "c", "d"):
        assert not (harness.workspace / f"{node_id}.txt").exists()
        effect = harness.store.effect_for("tx-1", 1, node_id)
        assert effect.phase == "compensated"

    # Idempotent: a second cascade returns the same terminal truth
    # without invoking any adapter again.
    calls_before = harness.adapter.commit_calls
    second = harness.coordinator.compensate("tx-1", "a", cascade=True)
    assert second.status == "compensated"
    assert harness.adapter.commit_calls == calls_before


def test_cascade_stops_before_changed_node_and_reports_partial(harness):
    _chain_harness(harness)
    # Human drift on 'b' after commit: d and c compensate, then the
    # cascade stops BEFORE b — never continuing across an unsafe node.
    (harness.workspace / "b.txt").write_text("human\n", encoding="utf-8")
    outcome = harness.coordinator.compensate("tx-1", "a", cascade=True)
    assert outcome.status == "partially_compensated"
    assert outcome.compensated_nodes == ("d", "c")
    assert harness.store.effect_for("tx-1", 1, "b").phase in {
        "committed", "verified",
    }
    assert harness.store.effect_for("tx-1", 1, "a").phase in {
        "committed", "verified",
    }
    assert harness.store.get_transaction("tx-1").status == "partially_compensated"


def test_compensation_without_cascade_raises_on_live_dependents(harness):
    _chain_harness(harness)
    with pytest.raises(EffectBlocked, match="dependent"):
        harness.coordinator.compensate("tx-1", "a")


def test_compensation_is_journaled_per_node(harness):
    harness.commit_graph([("write", "faketest.v1")])
    outcome = harness.coordinator.compensate("tx-1", "write")
    assert outcome.status == "compensated"
    attempts = harness.store.list_compensations("tx-1")
    assert len(attempts) == 1
    assert attempts[0].status == "compensated"
    assert attempts[0].fidelity == "exact"
    operation = harness.journal.get(attempts[0].operation_id)
    assert operation is not None
    assert operation.state == "confirmed"
