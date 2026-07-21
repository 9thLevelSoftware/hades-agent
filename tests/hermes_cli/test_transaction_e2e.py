"""Real-path E2E recovery, revision, and cascade proof (plan Task 13).

Uses a temp profile home, real ``state.db``, real config store, real
files/checkpoints, and — for the two post-handler fault boundaries — a
REAL subprocess killed by ``os._exit`` before the next durable
confirmation, followed by fresh object graphs in this process.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks.transactions.runner import TransactionCaseHarness

REPO_ROOT = Path(__file__).resolve().parents[2]

_CHILD_SCRIPT = r"""
import os, sys
from pathlib import Path

base, case_id, fault = sys.argv[1], sys.argv[2], sys.argv[3]
from benchmarks.transactions.runner import TransactionCaseHarness

harness = TransactionCaseHarness(Path(base), case_id)


def _hook(point, context):
    if point == fault:
        # Hard process death: no finally blocks, no journal cleanup.
        os._exit(9)


harness.coordinator._fault = _hook
tx = harness.create(case_id)
harness.coordinator.preview(tx)
harness.coordinator.commit(tx, invoke_map=harness.invoke_map())
# Reaching here means the fault never fired.
os._exit(3)
"""


def _run_child(base: Path, case_id: str, fault: str) -> int:
    env = dict(os.environ)
    env["HADES_HOME"] = str(base / case_id)
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT, str(base), case_id, fault],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
        timeout=180,
    )
    return proc.returncode


@pytest.mark.parametrize("fault_point", [
    "after_handler_return",
    "after_delivery_dispatch",
])
def test_process_kill_recovers_without_duplicate(tmp_path, fault_point):
    case_id = f"e2e-{fault_point.replace('_', '-')}"
    returncode = _run_child(tmp_path, case_id, fault_point)
    assert returncode == 9, f"child exited {returncode}, fault did not fire"

    # Fresh object graph in THIS process over the same durable home.
    harness = TransactionCaseHarness(tmp_path, case_id)
    try:
        moved = harness.journal.reconcile_after_restart(owner_fenced=True)
        assert moved >= 1, "dead-owner journal rows were not fenced"
        recovered = harness.coordinator.reconcile("tx-case")
        effect = harness.store.effect_for("tx-case", 1, "workspace_write")
        assert effect.phase in {"committed", "failed", "unknown_effect"}
        # The handler ran at most once: exactly one durable write exists
        # and its content matches exactly one invocation.
        note = harness.workspace / "notes" / "benchmark.md"
        if effect.phase == "committed":
            assert note.read_text(encoding="utf-8") == (
                f"benchmark write for {case_id}\n"
            )
        assert recovered.status in {
            "committed", "blocked", "unknown_effect",
        }
        assert harness.network_send_count("tx-case") == 0
    finally:
        harness.close()


@pytest.mark.parametrize("fault_point", [
    "after_prepare", "after_preview", "after_commit_intent",
])
def test_in_process_crashes_recover_without_duplicate(tmp_path, fault_point):
    case_id = f"e2e-{fault_point.replace('_', '-')}"
    harness = TransactionCaseHarness(tmp_path, case_id)
    try:
        tx = harness.create(case_id)
        harness.fault_point = (
            fault_point if fault_point in {"after_prepare", "after_preview"}
            else None
        )
        crashed = False
        try:
            harness.coordinator.preview(tx)
        except BaseException:
            crashed = True
        if not crashed:
            harness.fault_point = fault_point
            try:
                harness.coordinator.commit(tx, invoke_map=harness.invoke_map())
            except BaseException:
                crashed = True
        assert crashed
        harness.restart()
        harness.fault_point = None
        harness.coordinator.reconcile(tx)
        assert harness.handler_calls <= 1
        assert harness.network_send_count(tx) == 0
    finally:
        harness.close()


def test_revision_and_cascade_end_to_end(tmp_path):
    harness = TransactionCaseHarness(tmp_path, "e2e-revision")
    try:
        tx = harness.create("e2e-revision")
        harness.coordinator.preview(tx)
        harness.coordinator.commit(
            tx, through_node="workspace_write", invoke_map=harness.invoke_map(),
        )
        frozen_v1 = harness.store.get_node(tx, 1, "workspace_write")

        revised = harness.graph("e2e-revision", message="corrected")
        harness.coordinator.revise(
            tx, expected_revision=1, graph=revised, reason="corrected message",
        )
        harness.coordinator.preview(tx)
        result = harness.coordinator.commit(tx, invoke_map=harness.invoke_map())
        assert result.status == "committed"

        # Only the corrected message exists in the durable outbox.
        from gateway.mission_outbox import MissionOutboxStore

        outbox = MissionOutboxStore(harness.db)
        effect = harness.store.effect_for(tx, 2, "delayed_message")
        token = (effect.prepared or {}).get("prepared_token") or {}
        row = outbox.get_by_id(token["outbox_id"])
        assert "corrected" in json.dumps(row.content)

        # The frozen committed node is identical across revisions.
        assert harness.store.get_node(tx, 2, "workspace_write") == frozen_v1

        outcome = harness.coordinator.compensate(
            tx, "workspace_write", cascade=True,
        )
        assert outcome.status == "compensated"
        assert outcome.compensated_nodes == (
            "delayed_message", "config_set", "workspace_write",
        )
    finally:
        harness.close()


def test_tool_schema_and_definitions_stay_byte_stable(tmp_path):
    """Effective model tool definitions never change across transaction
    state transitions (plan invariant step)."""
    from model_tools import registry

    def _digest() -> str:
        definitions = registry.get_definitions({"write_file", "patch"})
        return hashlib.sha256(
            json.dumps(definitions, sort_keys=True, default=str).encode()
        ).hexdigest()

    before = _digest()
    harness = TransactionCaseHarness(tmp_path, "e2e-invariants")
    try:
        tx = harness.create("e2e-invariants")
        harness.coordinator.preview(tx)
        after_preview = _digest()
        harness.coordinator.commit(tx, invoke_map=harness.invoke_map())
        after_commit = _digest()
        harness.coordinator.reconcile(tx)
        after_reconcile = _digest()
    finally:
        harness.close()
    assert before == after_preview == after_commit == after_reconcile
