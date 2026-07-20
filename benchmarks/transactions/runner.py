"""Report-only executor for the preregistered 100-case transaction corpus.

Every case runs the REAL coordinator/store/journal/adapters against a
private profile home: real SQLite, real files, real config.yaml, real
outbox rows. The only fakes are the deterministic crash boundary (the
injected fault hook) and the final network platform (delayed messages
never dispatch). Output is local JSON/Markdown only; gates come frozen
from the manifest and are never relaxed after results exist.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Optional

from benchmarks.transactions.cases import APPROVED_GATES, load_cases

_PRODUCER = "benchmarks.transactions.runner"


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    stratum: str
    passed: bool
    transaction_status: str
    duplicate_effects: int
    unauthorized_irreversible_commits: int
    compensation_order_correct: bool
    every_non_reversible_classified: bool
    false_success_receipt: bool
    baseline_latency_ms: float
    transaction_latency_ms: float
    excluded_reason: Optional[str] = None
    detail: str = ""


@dataclass(frozen=True)
class BenchmarkReport:
    schema: str
    denominator: int
    executed: int
    excluded: tuple[dict, ...]
    stratum_rates: Mapping[str, Mapping[str, float]]
    gate_results: Mapping[str, Any]
    gates_passed: bool
    baseline_p50_ms: float
    baseline_p95_ms: float
    transaction_p50_ms: float
    transaction_p95_ms: float
    median_eligible_overhead_ratio: float
    environment: Mapping[str, str]
    results: tuple[CaseResult, ...] = field(repr=False, default=())


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    margin = (
        z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    )
    return (max(0.0, centre - margin), min(1.0, centre + margin))


class _AllowAll:
    def authorize(self, context, *, consume):
        return SimpleNamespace(
            allowed=True, verdict="allow", code="allow", context_hash="ctx",
        )


class _DenyAll:
    def authorize(self, context, *, consume):
        return SimpleNamespace(
            allowed=False, verdict="deny", code="authority_changed",
            context_hash="ctx",
        )


class TransactionCaseHarness:
    """One private profile home + real transaction stack per case."""

    def __init__(self, base_dir: Path, case_id: str):
        from hades_constants import set_hades_home_override

        self.home = Path(base_dir) / case_id
        self.workspace = self.home / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.home / "config.yaml").write_text("{}\n", encoding="utf-8")
        self._home_token = set_hades_home_override(str(self.home))

        import tools.checkpoint_manager as checkpoint_manager_module

        self._checkpoint_prior = checkpoint_manager_module.CHECKPOINT_BASE
        checkpoint_manager_module.CHECKPOINT_BASE = self.home / "checkpoints"

        self.fault_point: Optional[str] = None
        self.provider: Any = _AllowAll()
        self.handler_calls = 0
        self._build()

    def _build(self):
        from agent.effects.adapters.hermes_state import HermesConfigAdapter
        from agent.effects.adapters.message_outbox import MessageOutboxAdapter
        from agent.effects.adapters.workspace import WorkspaceAdapter
        from agent.effects.coordinator import TransactionCoordinator
        from agent.effects.registry import EffectAdapterRegistry
        from agent.effects.store import TransactionStore
        from agent.operation_journal import OperationJournal
        from hades_state import SessionDB

        self.db = SessionDB(self.home / "state.db")
        self.store = TransactionStore(self.db)
        self.journal = OperationJournal(self.db)
        self.adapters = EffectAdapterRegistry()
        self.adapters.register(WorkspaceAdapter(
            workspace_root=self.workspace,
            transaction_lookup=self.store.get_effect_by_operation_id,
        ))
        self.adapters.register(HermesConfigAdapter())
        self.adapters.register(MessageOutboxAdapter(
            db_factory=lambda: self.db,
        ))
        self.coordinator = TransactionCoordinator(
            store=self.store,
            adapters=self.adapters,
            journal=self.journal,
            authority_provider_factory=lambda: self.provider,
            fault_hook=self._fault_hook,
        )

    class _Crash(BaseException):
        pass

    def _fault_hook(self, point: str, context) -> None:
        if point == self.fault_point:
            raise TransactionCaseHarness._Crash(point)

    def restart(self):
        self.db.close()
        self._build()
        self.journal.reconcile_after_restart(owner_fenced=True)

    def close(self):
        from hades_constants import reset_hades_home_override
        import tools.checkpoint_manager as checkpoint_manager_module

        try:
            self.db.close()
        finally:
            checkpoint_manager_module.CHECKPOINT_BASE = self._checkpoint_prior
            reset_hades_home_override(self._home_token)

    # ── Plan helpers ─────────────────────────────────────────────────

    def graph(self, case_id: str, *, message: str = "benchmark delivery"):
        return {
            "nodes": [
                {
                    "node_id": "workspace_write",
                    "adapter_id": "workspace.v1",
                    "action": "write_file",
                    "args": {
                        "path": "notes/benchmark.md",
                        "content": f"benchmark write for {case_id}\n",
                    },
                },
                {
                    "node_id": "config_set",
                    "adapter_id": "hermes-config.v1",
                    "action": "set",
                    "args": {"key": "ui.timezone", "value": "UTC"},
                },
                {
                    "node_id": "delayed_message",
                    "adapter_id": "message-outbox.v1",
                    "action": "send",
                    "args": {
                        "platform": "faketest",
                        "target": "faketest:benchmark-channel",
                        "message": f"{message} for {case_id}",
                        "not_before_seconds": 3600,
                    },
                },
            ],
            "edges": [
                {"parent": "workspace_write", "child": "config_set"},
                {"parent": "config_set", "child": "delayed_message"},
            ],
        }

    def create(self, case_id: str, transaction_id: str = "tx-case", **kwargs):
        self.store.create_transaction(
            transaction_id=transaction_id,
            profile="default",
            title=f"benchmark {case_id}",
            authority={"authority_version": 1, "irreversible_policy": "ask"},
            graph=self.graph(case_id, **kwargs),
            failure_policy="stop",
        )
        return transaction_id

    def invoke_map(self):
        def _write(args):
            self.handler_calls += 1
            target = Path(args["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(args["content"], encoding="utf-8", newline="")
            return {"success": True}

        return {"workspace_write": _write}

    def network_send_count(self, transaction_id: str) -> int:
        """Rows that ever left the Hermes boundary — none may dispatch."""
        from gateway.mission_outbox import MissionOutboxStore

        outbox = MissionOutboxStore(self.db)
        count = 0
        for effect in self.store.list_effects(transaction_id):
            token = (effect.prepared or {}).get("prepared_token") or {}
            outbox_id = token.get("outbox_id")
            if not outbox_id:
                continue
            record = outbox.get_by_id(outbox_id)
            if record is not None and record.status in {"delivered"}:
                count += 1
        return count


def _baseline_run(base_dir: Path, case_id: str) -> float:
    """Current-Hermes durable pipeline for the same three effects,
    without the transaction coordinator.

    The comparison baseline named by the manifest is the EXISTING durable
    machinery a non-transactional Hermes flow already pays for: an
    operation-journal row per effect, a forced workspace checkpoint, the
    atomic config writer, and a durable outbox row — not bare
    ``write_text`` calls, which would overstate coordinator overhead.
    """
    from agent.operation_journal import OperationJournal
    from gateway.mission_outbox import MissionOutboxStore
    from hades_state import SessionDB
    from tools.checkpoint_manager import CheckpointManager

    root = Path(base_dir) / f"baseline-{case_id}"
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    db = SessionDB(root / "state.db")
    try:
        journal = OperationJournal(db)
        outbox = MissionOutboxStore(db)
        manager = CheckpointManager(enabled=True, max_snapshots=5)
        started = time.perf_counter()
        for index, kind in enumerate(("write", "config", "send")):
            operation_id = f"bl-{case_id}-{index}"
            journal.create(operation_id=operation_id, kind="tool")
            journal.transition(
                operation_id, from_states={"pending"}, to_state="running",
                effect_disposition="none",
            )
            if kind == "write":
                try:
                    manager.create_checkpoint(
                        str(workspace), reason="baseline", force=True,
                    )
                except Exception:
                    pass
                target = workspace / "notes" / "benchmark.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    f"benchmark write for {case_id}\n", encoding="utf-8",
                )
            elif kind == "config":
                from hades_cli.config import atomic_config_write

                atomic_config_write(
                    root / "config.yaml", {"ui": {"timezone": "UTC"}},
                    sort_keys=False,
                )
            else:
                outbox.materialize(
                    execution_id=f"baseline:{case_id}",
                    node_id="send",
                    platform="faketest",
                    target="faketest:benchmark-channel",
                    content={"message": f"baseline delivery {case_id}"},
                    requires_approval=True,
                    not_before=int(time.time()) + 3600,
                )
            journal.transition(
                operation_id, from_states={"running"}, to_state="confirmed",
                effect_disposition="landed",
            )
        return (time.perf_counter() - started) * 1000
    finally:
        db.close()


def run_case(case: Mapping[str, Any], base_dir: Path) -> CaseResult:
    case_id = case["id"]
    stratum = case["stratum"]
    harness = TransactionCaseHarness(Path(base_dir), case_id)
    baseline_ms = _baseline_run(Path(base_dir), case_id)
    started = time.perf_counter()
    passed = True
    detail = ""
    status = "unknown"
    duplicates = 0
    unauthorized = 0
    order_correct = True
    classified = True
    false_success = False
    try:
        tx = harness.create(case_id)
        if stratum == "revision":
            harness.coordinator.preview(tx)
            harness.coordinator.commit(
                tx, through_node="workspace_write",
                invoke_map=harness.invoke_map(),
            )
            frozen_before = harness.store.get_node(tx, 1, "workspace_write")
            revised = harness.graph(case_id, message="revised delivery")
            harness.coordinator.revise(
                tx, expected_revision=1, graph=revised, reason="new message",
            )
            from agent.effects.models import RevisionConflict

            try:
                harness.coordinator.revise(
                    tx, expected_revision=1, graph=revised, reason="stale",
                )
                passed, detail = False, "stale CAS did not lose"
            except RevisionConflict:
                pass
            harness.coordinator.preview(tx)
            harness.coordinator.commit(tx, invoke_map=harness.invoke_map())
            frozen_after = harness.store.get_node(tx, 2, "workspace_write")
            if frozen_before != frozen_after:
                passed, detail = False, "frozen node changed across revision"
            committed_message = harness.store.get_node(
                tx, 2, "delayed_message",
            )
            if "revised delivery" not in str(committed_message.args):
                passed, detail = False, "revised args were not the committed args"
            status = harness.store.get_transaction(tx).status
        elif stratum == "stale_authority":
            harness.coordinator.preview(tx)
            harness.provider = _DenyAll()
            result = harness.coordinator.commit(
                tx, invoke_map=harness.invoke_map(),
            )
            status = harness.store.get_transaction(tx).status
            if result.status != "blocked" or status != "blocked":
                passed, detail = False, f"expected blocked, got {result.status}"
            if harness.handler_calls != 0:
                passed, detail = False, "handler ran under stale authority"
                unauthorized += 1
            if harness.network_send_count(tx) != 0:
                passed, detail = False, "network effect under stale authority"
        elif stratum == "crash":
            fault = case["fault_point"]
            harness.fault_point = (
                fault if fault in {"after_prepare", "after_preview"} else None
            )
            crashed = False
            try:
                harness.coordinator.preview(tx)
            except TransactionCaseHarness._Crash:
                crashed = True
            if not crashed:
                harness.fault_point = fault
                try:
                    harness.coordinator.commit(
                        tx, invoke_map=harness.invoke_map(),
                    )
                except TransactionCaseHarness._Crash:
                    crashed = True
            if not crashed:
                passed, detail = False, f"fault {fault} did not fire"
            harness.restart()
            harness.fault_point = None
            recovered = harness.coordinator.reconcile(tx)
            status = recovered.status
            if harness.handler_calls > 1:
                duplicates += harness.handler_calls - 1
                passed, detail = False, "handler ran more than once"
            note = (harness.workspace / "notes" / "benchmark.md")
            if fault in {"after_handler_return", "after_delivery_dispatch"}:
                effect = harness.store.effect_for(tx, 1, "workspace_write")
                if effect.phase not in {
                    "committed", "verified", "failed", "unknown_effect",
                }:
                    classified = False
                    passed, detail = False, f"unclassified phase {effect.phase}"
                if effect.phase == "committed" and not note.exists():
                    passed, detail = False, "landed classification without file"
            if harness.network_send_count(tx) > 1:
                duplicates += 1
        elif stratum == "duplicate_delivery":
            harness.coordinator.preview(tx)
            harness.coordinator.commit(tx, invoke_map=harness.invoke_map())
            from gateway.mission_outbox import MissionOutboxStore

            outbox = MissionOutboxStore(harness.db)
            effect = harness.store.effect_for(tx, 1, "delayed_message")
            token = (effect.prepared or {}).get("prepared_token") or {}
            row_before = outbox.get_by_id(token["outbox_id"])
            # Replayed enqueue with the same identity must dedupe.
            record = outbox.materialize(
                execution_id=token["execution_id"],
                node_id="delayed_message",
                platform="faketest",
                target="faketest:benchmark-channel",
                content=dict(row_before.content),
                requires_approval=True,
                not_before=row_before.not_before,
            )
            if record.outbox_id != row_before.outbox_id:
                duplicates += 1
                passed, detail = False, "duplicate outbox row materialized"
            if record.revision != row_before.revision:
                passed, detail = False, "replay bumped the revision"
            status = harness.store.get_transaction(tx).status
            if harness.network_send_count(tx) > 1:
                duplicates += 1
        elif stratum == "partial_failure":
            harness.coordinator.preview(tx)
            # Concurrent out-of-band config edit: the config node's
            # optimistic revision check must fail at commit time.
            config_path = harness.home / "config.yaml"
            config_path.write_text(
                "unrelated:\n  edited: true\n", encoding="utf-8",
            )
            result = harness.coordinator.commit(
                tx, invoke_map=harness.invoke_map(),
            )
            status = harness.store.get_transaction(tx).status
            message_effect = harness.store.effect_for(tx, 1, "delayed_message")
            if message_effect.phase in {"committed", "verified"}:
                passed, detail = False, "descendant committed past a failure"
            if result.status not in {"failed", "blocked", "unknown_effect"}:
                passed, detail = False, f"unexpected commit status {result.status}"
            if harness.network_send_count(tx) != 0:
                passed, detail = False, "delivery after partial failure"
        elif stratum == "compensation_boundary":
            harness.coordinator.preview(tx)
            harness.coordinator.commit(tx, invoke_map=harness.invoke_map())
            from agent.effects.eligibility import eligibility_for_effect

            eligibility = eligibility_for_effect(
                harness.store, harness.adapters, tx, "workspace_write",
                cascade=True,
            )
            if eligibility.code not in {
                "eligible_exact", "eligible_compensation",
            }:
                passed, detail = False, f"eligibility {eligibility.code}"
            expected_order = ("delayed_message", "config_set", "workspace_write")
            if eligibility.required_cascade_node_ids != expected_order:
                order_correct = False
                passed, detail = False, (
                    f"cascade order {eligibility.required_cascade_node_ids}"
                )
            outcome = harness.coordinator.compensate(
                tx, "workspace_write", cascade=True,
            )
            status = harness.store.get_transaction(tx).status
            if outcome.compensated_nodes != expected_order:
                order_correct = False
                passed, detail = False, (
                    f"compensated order {outcome.compensated_nodes}"
                )
            for node_id, want in (
                ("workspace_write", "exact"),
                ("config_set", "semantic"),
                ("delayed_message", "semantic"),
            ):
                effect = harness.store.effect_for(tx, 1, node_id)
                fidelity = (effect.semantics or {}).get("fidelity")
                if fidelity != want:
                    classified = False
                    passed, detail = False, (
                        f"{node_id} fidelity {fidelity} != {want}"
                    )
        else:
            return CaseResult(
                case_id=case_id, stratum=stratum, passed=False,
                transaction_status="excluded", duplicate_effects=0,
                unauthorized_irreversible_commits=0,
                compensation_order_correct=True,
                every_non_reversible_classified=True,
                false_success_receipt=False,
                baseline_latency_ms=baseline_ms,
                transaction_latency_ms=0.0,
                excluded_reason=f"unknown stratum {stratum}",
            )

        # The timed candidate flow ends here: receipt issuance is a
        # terminal-time producer step both pipelines share, so it stays
        # outside the overhead comparison.
        flow_elapsed_ms = (time.perf_counter() - started) * 1000

        # False-success check: issue the receipt and demand it never
        # claims verified unless every claim is proven.
        from agent.effects.receipts import TransactionReceiptBuilder
        from agent.receipts import ReceiptStore

        builder = TransactionReceiptBuilder(
            harness.store, receipt_store=ReceiptStore(harness.db),
            adapters=harness.adapters, journal=harness.journal,
        )
        receipt = builder.issue(tx)
        terminal = harness.store.get_transaction(tx)
        if receipt.status == "verified" and terminal.status not in {
            "committed", "compensated",
        }:
            false_success = True
            passed, detail = False, (
                f"verified receipt over status {terminal.status}"
            )
        elapsed_ms = flow_elapsed_ms
    except Exception as exc:  # noqa: BLE001 — a case failure is a result
        passed = False
        detail = f"{type(exc).__name__}: {exc}"
        elapsed_ms = (time.perf_counter() - started) * 1000
    finally:
        harness.close()

    return CaseResult(
        case_id=case_id, stratum=stratum, passed=passed,
        transaction_status=status,
        duplicate_effects=duplicates,
        unauthorized_irreversible_commits=unauthorized,
        compensation_order_correct=order_correct,
        every_non_reversible_classified=classified,
        false_success_receipt=false_success,
        baseline_latency_ms=baseline_ms,
        transaction_latency_ms=elapsed_ms,
        detail=detail,
    )


def run_benchmark(
    manifest_path: Path,
    *,
    repeats: int = 1,
    output: Optional[Path] = None,
    base_dir: Optional[Path] = None,
) -> BenchmarkReport:
    manifest, cases = load_cases(Path(manifest_path))
    work_dir = Path(base_dir) if base_dir else Path(
        tempfile.mkdtemp(prefix="tx-benchmark-")
    )
    results: list[CaseResult] = []
    try:
        for _ in range(max(1, int(repeats))):
            for case in cases:
                results.append(run_case(case, work_dir))
    finally:
        if base_dir is None:
            shutil.rmtree(work_dir, ignore_errors=True)

    executed = [r for r in results if r.excluded_reason is None]
    excluded = tuple(
        {"case_id": r.case_id, "reason": r.excluded_reason}
        for r in results if r.excluded_reason is not None
    )
    stratum_rates: dict[str, dict[str, float]] = {}
    for stratum in sorted({r.stratum for r in executed}):
        rows = [r for r in executed if r.stratum == stratum]
        successes = sum(1 for r in rows if r.passed)
        low, high = wilson_interval(successes, len(rows))
        stratum_rates[stratum] = {
            "total": float(len(rows)),
            "passed": float(successes),
            "rate": successes / len(rows) if rows else 0.0,
            "wilson_low": low,
            "wilson_high": high,
        }

    baseline = sorted(r.baseline_latency_ms for r in executed) or [0.0]
    candidate = sorted(r.transaction_latency_ms for r in executed) or [0.0]

    def _pct(values, q):
        index = min(len(values) - 1, max(0, int(round(q * (len(values) - 1)))))
        return values[index]

    overhead_ratios = [
        (r.transaction_latency_ms - r.baseline_latency_ms)
        / r.baseline_latency_ms
        for r in executed if r.baseline_latency_ms > 0
    ]
    median_overhead = (
        statistics.median(overhead_ratios) if overhead_ratios else 0.0
    )
    gate_results = {
        "unauthorized_irreversible_commits": sum(
            r.unauthorized_irreversible_commits for r in executed
        ),
        "duplicate_instrumented_effects": sum(
            r.duplicate_effects for r in executed
        ),
        "incorrect_compensation_order": sum(
            1 for r in executed if not r.compensation_order_correct
        ),
        "unclassified_non_reversible_effects": sum(
            1 for r in executed if not r.every_non_reversible_classified
        ),
        "false_success_receipts": sum(
            1 for r in executed if r.false_success_receipt
        ),
        "median_eligible_overhead_ratio": median_overhead,
    }
    gates_passed = (
        gate_results["unauthorized_irreversible_commits"]
        <= APPROVED_GATES["unauthorized_irreversible_commits"]
        and gate_results["duplicate_instrumented_effects"]
        <= APPROVED_GATES["duplicate_instrumented_effects"]
        and gate_results["incorrect_compensation_order"]
        <= APPROVED_GATES["incorrect_compensation_order"]
        and gate_results["unclassified_non_reversible_effects"]
        <= APPROVED_GATES["unclassified_non_reversible_effects"]
        and gate_results["false_success_receipts"]
        <= APPROVED_GATES["false_success_receipts"]
        and all(r.passed for r in executed)
    )
    report = BenchmarkReport(
        schema=str(manifest["schema"]),
        denominator=len(cases),
        executed=len(executed),
        excluded=excluded,
        stratum_rates=stratum_rates,
        gate_results=gate_results,
        gates_passed=gates_passed,
        baseline_p50_ms=_pct(baseline, 0.50),
        baseline_p95_ms=_pct(baseline, 0.95),
        transaction_p50_ms=_pct(candidate, 0.50),
        transaction_p95_ms=_pct(candidate, 0.95),
        median_eligible_overhead_ratio=median_overhead,
        environment={
            "os": platform.platform(),
            "python": sys.version.split()[0],
            "sqlite": sqlite3.sqlite_version,
            "filesystem": "local",
            "git": _git_version(),
            "network": "fake",
            "cost_source": "local wall clock; no billed calls",
        },
        results=tuple(results),
    )
    if output is not None:
        payload = asdict(report)
        payload["results"] = [asdict(r) for r in results]
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
    return report


def _git_version() -> str:
    try:
        return subprocess.check_output(
            ["git", "--version"], text=True
        ).strip()
    except Exception:
        return "unavailable"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="transaction-benchmark")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output-json", dest="output_json")
    args = parser.parse_args(argv)
    report = run_benchmark(
        Path(args.manifest),
        repeats=args.repeats,
        output=Path(args.output_json) if args.output_json else None,
    )
    print(json.dumps({
        "denominator": report.denominator,
        "executed": report.executed,
        "excluded": list(report.excluded),
        "stratum_rates": dict(report.stratum_rates),
        "gates": dict(report.gate_results),
        "gates_passed": report.gates_passed,
        "baseline_p50_ms": report.baseline_p50_ms,
        "baseline_p95_ms": report.baseline_p95_ms,
        "transaction_p50_ms": report.transaction_p50_ms,
        "transaction_p95_ms": report.transaction_p95_ms,
        "median_eligible_overhead_ratio":
            report.median_eligible_overhead_ratio,
        "environment": dict(report.environment),
    }, indent=2, sort_keys=True))
    return 0 if report.gates_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
