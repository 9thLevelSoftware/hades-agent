"""Shared CLI surface for reversible action transactions (plan Task 11).

One ``run_argv()`` service path backs the top-level ``hades transaction``
command, the classic ``/transaction`` slash route, the native TUI RPC,
and tests. Plan/authority YAML is read only from explicit paths, capped
at 1 MiB; JSON output redacts content but preserves hashes and stable
ids; eligibility output never abbreviates blocker reasons.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shlex
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional, Sequence

import yaml

from hades_constants import get_hades_home

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_VALIDATION = 2

_MAX_ARGS = 64
_MAX_TOTAL_ARG_BYTES = 64 * 1024
_MAX_PLAN_BYTES = 1024 * 1024

_MODES = ("off", "preview", "commit")
# Subcommands allowed per mode; anything mutating outward requires
# ``commit``. ``off`` keeps only recovery/inspection reads working.
_PREVIEW_ALLOWED = frozenset({
    "create", "list", "show", "graph", "preview", "revise", "reconcile",
    "eligibility", "receipt", "outbox",
})
_READ_ONLY = frozenset({"list", "show", "graph", "eligibility", "receipt"})


class _CliUsageError(RuntimeError):
    pass


class _TransactionArgumentParser(argparse.ArgumentParser):
    def error(self, message):  # noqa: A003 — argparse contract
        raise _CliUsageError(message)


@dataclass(frozen=True)
class TransactionCommandResult:
    exit_code: int
    output: str
    payload: dict


def _usage(message: str) -> _CliUsageError:
    return _CliUsageError(message)


def _configured_mode() -> str:
    try:
        from hades_cli.config import load_config_readonly

        cfg = (load_config_readonly() or {}).get("transactions") or {}
        mode = cfg.get("mode")
        if isinstance(mode, str) and mode in _MODES:
            return mode
    except Exception:
        pass
    return "preview"


def _load_bounded_yaml(path_value: str, label: str) -> dict:
    path = Path(path_value)
    if not path.is_file():
        raise _usage(f"{label} file not found: {path}")
    raw = path.read_bytes()
    if len(raw) > _MAX_PLAN_BYTES:
        raise _usage(f"{label} file exceeds the 1 MiB bound: {path}")
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except Exception as exc:
        raise _usage(f"{label} file is not valid YAML: {exc}")
    if not isinstance(data, dict):
        raise _usage(f"{label} file must contain a mapping")
    return data


# ── Service construction ────────────────────────────────────────────────


class _Services:
    def __init__(self):
        from agent.effects.adapters import register_builtin_adapters
        from agent.effects.adapters.message_outbox import MessageOutboxAdapter
        from agent.effects.authority import consume_bound_approval
        from agent.effects.coordinator import TransactionCoordinator
        from agent.effects.registry import EffectAdapterRegistry
        from agent.effects.store import TransactionStore
        from agent.operation_journal import OperationJournal
        from hades_state import SessionDB

        self.db = SessionDB(get_hades_home() / "state.db")
        self.store = TransactionStore(self.db)
        self.journal = OperationJournal(self.db)
        self.adapters = EffectAdapterRegistry()
        register_builtin_adapters(
            self.adapters,
            workspace_root=Path.cwd(),
            transaction_lookup=self.store.get_effect_by_operation_id,
        )
        self.adapters.register(MessageOutboxAdapter(db_factory=lambda: self.db))

        def _provider():
            # Mirror the runtime authority_gate: autonomy mode "off"
            # means no profile evaluator is configured. The transaction's
            # own stored contract and exact approvals still bind.
            try:
                from agent.autonomy.runtime import _load_autonomy_section

                if _load_autonomy_section().get("mode", "off") == "off":
                    return None
            except Exception:
                pass  # unreadable section: enforce (fail closed)
            from agent.autonomy import StoredAuthorityProvider

            return StoredAuthorityProvider(db=self.db)

        def _approval_consumer(**identity):
            from agent.effects.authority import ApprovalIdentity

            return consume_bound_approval(
                self.store,
                ApprovalIdentity(
                    transaction_id=identity["transaction_id"],
                    revision=identity["revision"],
                    node_id=identity["node_id"],
                    operation="commit",
                    args_hash=identity["args_hash"],
                    preview_hash=identity["preview_hash"],
                    resources=tuple(sorted(identity["resources"])),
                    authority_version=identity["authority_version"],
                    requester=identity["requester"],
                    channel=identity["channel"],
                ),
            )

        self.coordinator = TransactionCoordinator(
            store=self.store,
            adapters=self.adapters,
            journal=self.journal,
            authority_provider_factory=_provider,
            approval_consumer=_approval_consumer,
        )

    def receipt_builder(self):
        from agent.effects.receipts import TransactionReceiptBuilder
        from agent.receipts import ReceiptStore

        return TransactionReceiptBuilder(
            self.store,
            receipt_store=ReceiptStore(self.db),
            adapters=self.adapters,
            journal=self.journal,
        )

    def outbox(self):
        from gateway.mission_outbox import MissionOutboxStore

        return MissionOutboxStore(self.db)

    def close(self):
        try:
            self.db.close()
        except Exception:
            pass


# ── Rendering ───────────────────────────────────────────────────────────


def _transaction_json(transaction) -> dict:
    return {
        "transaction_id": transaction.transaction_id,
        "status": transaction.status,
        "current_revision": transaction.current_revision,
        "authority_version": transaction.authority_version,
        "failure_policy": transaction.failure_policy,
        "receipt_id": transaction.receipt_id,
        "title": transaction.title,
    }


def _node_row(store, transaction_id, revision, node) -> dict:
    effect = store.effect_for(transaction_id, revision, node.node_id)
    semantics = dict(effect.semantics or {}) if effect else {}
    return {
        "node_id": node.node_id,
        "adapter_id": node.adapter_id,
        "action": node.action,
        "phase": effect.phase if effect else "planned",
        "fidelity": semantics.get("fidelity", "unknown"),
        "preview_hash": effect.preview_hash if effect else None,
        # Content is deliberately absent from JSON output: hashes and
        # stable ids only.
        "args_hash": None if effect is None else (
            (effect.prepared or {}).get("expected_after") and "bound" or "bound"
        ),
    }


def _render_nodes_table(rows) -> str:
    lines = [
        f"{'node':<20} {'adapter':<22} {'phase':<14} {'fidelity':<10}",
    ]
    for row in rows:
        lines.append(
            f"{row['node_id']:<20} {row['adapter_id']:<22} "
            f"{row['phase']:<14} {row['fidelity']:<10}"
        )
    return "\n".join(lines)


# ── Command handlers ────────────────────────────────────────────────────


def _cmd_create(services: _Services, args) -> TransactionCommandResult:
    plan = _load_bounded_yaml(args.plan, "plan")
    authority = _load_bounded_yaml(args.authority, "authority")
    graph = {"nodes": plan.get("nodes") or [], "edges": plan.get("edges") or []}
    meta = plan.get("transaction") or {}
    transaction_id = args.transaction_id or f"tx-{os.urandom(6).hex()}"
    from agent.effects.graph import validate_graph

    validate_graph(graph, adapter_registry=services.adapters)
    record = services.store.create_transaction(
        transaction_id=transaction_id,
        profile="default",
        title=str(meta.get("title") or "action transaction"),
        authority=authority,
        graph=graph,
        failure_policy=str(meta.get("failure_policy") or "stop"),
    )
    revision = services.store.get_revision(transaction_id, 1)
    classifications = sorted({
        f"{node.adapter_id}:{node.action}" for node in revision.nodes
    })
    output = "\n".join([
        f"created transaction {record.transaction_id} (revision 1)",
        f"effects: {', '.join(classifications)}",
        f"authority version {record.authority_version}, expires_at_ms="
        f"{authority.get('expires_at_ms', 'unset')}",
        f"next: hermes transaction preview {record.transaction_id}",
    ])
    return TransactionCommandResult(EXIT_OK, output, {
        "ok": True, "action": "create",
        "transaction": _transaction_json(record),
    })


def _cmd_list(services: _Services, args) -> TransactionCommandResult:
    from agent.effects.models import TRANSACTION_STATUSES

    statuses = (
        {args.status} if args.status else set(TRANSACTION_STATUSES)
    )
    rows = services.store.list_transactions_by_status(statuses)
    lines = [f"{'transaction':<28} {'status':<22} {'rev':<4} receipt"]
    for row in rows:
        lines.append(
            f"{row.transaction_id:<28} {row.status:<22} "
            f"{row.current_revision:<4} {row.receipt_id or '-'}"
        )
    return TransactionCommandResult(
        EXIT_OK,
        "\n".join(lines) if rows else "no transactions",
        {
            "ok": True, "action": "list",
            "transactions": [_transaction_json(row) for row in rows],
        },
    )


def _require_transaction(services: _Services, transaction_id: str):
    transaction = services.store.get_transaction(transaction_id)
    if transaction is None:
        raise _usage(f"unknown transaction {transaction_id!r}")
    return transaction


def _cmd_show(services: _Services, args) -> TransactionCommandResult:
    transaction = _require_transaction(services, args.transaction_id)
    revision = services.store.get_revision(
        transaction.transaction_id, transaction.current_revision
    )
    rows = [
        _node_row(
            services.store, transaction.transaction_id, revision.revision,
            node,
        )
        for node in revision.nodes
    ]
    output = "\n".join([
        f"transaction {transaction.transaction_id}: {transaction.status} "
        f"(revision {revision.revision})",
        f"graph {revision.graph_hash[:16]} preview "
        f"{(revision.preview_hash or '-')[:16]}",
        _render_nodes_table(rows),
    ])
    return TransactionCommandResult(EXIT_OK, output, {
        "ok": True, "action": "show",
        "transaction": _transaction_json(transaction),
        "nodes": rows,
    })


def _cmd_graph(services: _Services, args) -> TransactionCommandResult:
    transaction = _require_transaction(services, args.transaction_id)
    revision_number = args.revision or transaction.current_revision
    revision = services.store.get_revision(
        transaction.transaction_id, revision_number
    )
    if revision is None:
        raise _usage(
            f"transaction {transaction.transaction_id!r} has no revision "
            f"{revision_number}"
        )
    edges = [
        f"{edge.parent_node_id} -> {edge.child_node_id}"
        for edge in revision.edges
    ]
    output = "\n".join([
        f"revision {revision.revision} (base "
        f"{revision.base_revision or '-'}): {revision.reason}",
        *[f"node {node.node_id} [{node.adapter_id}:{node.action}]"
          for node in revision.nodes],
        *edges,
    ])
    return TransactionCommandResult(EXIT_OK, output, {
        "ok": True, "action": "graph",
        "revision": revision.revision,
        "nodes": [node.node_id for node in revision.nodes],
        "edges": edges,
    })


def _cmd_preview(services: _Services, args) -> TransactionCommandResult:
    _require_transaction(services, args.transaction_id)
    result = services.coordinator.preview(args.transaction_id)
    if result.status != "ready":
        return TransactionCommandResult(EXIT_ERROR, (
            f"preview blocked: {result.error}"
        ), {"ok": False, "action": "preview", "error": result.error})
    transaction = services.store.get_transaction(args.transaction_id)
    revision = services.store.get_revision(
        args.transaction_id, transaction.current_revision
    )
    lines = [
        f"preview ready (hash {result.preview_hash[:16]})",
        f"{'node':<20} {'fidelity':<12} {'approval':<9} summary",
    ]
    rows = []
    for node in revision.nodes:
        effect = services.store.effect_for(
            args.transaction_id, revision.revision, node.node_id,
        )
        preview = dict(effect.preview or {})
        semantics = dict(effect.semantics or {})
        summary = str(preview.get("summary", "")).splitlines()
        rows.append({
            "node_id": node.node_id,
            "fidelity": semantics.get("fidelity"),
            "requires_approval": bool(preview.get("requires_approval")),
            "summary": preview.get("summary", ""),
            "before": preview.get("before"),
            "after": preview.get("after"),
        })
        lines.append(
            f"{node.node_id:<20} {semantics.get('fidelity', '?'):<12} "
            f"{'yes' if preview.get('requires_approval') else 'no':<9} "
            f"{summary[0] if summary else ''}"
        )
    return TransactionCommandResult(EXIT_OK, "\n".join(lines), {
        "ok": True, "action": "preview",
        "preview_hash": result.preview_hash,
        "nodes": rows,
    })


def _cmd_revise(services: _Services, args) -> TransactionCommandResult:
    plan = _load_bounded_yaml(args.plan, "plan")
    graph = {"nodes": plan.get("nodes") or [], "edges": plan.get("edges") or []}
    record = services.coordinator.revise(
        args.transaction_id,
        expected_revision=args.expected_revision,
        graph=graph,
        reason=args.reason,
    )
    return TransactionCommandResult(EXIT_OK, (
        f"revision {record.revision} created; run preview before commit"
    ), {"ok": True, "action": "revise", "revision": record.revision})


def _workspace_invoke_map(services: _Services, transaction) -> dict:
    """Registered terminal tool handlers for workspace nodes.

    The workspace adapter refuses to mutate without the real handler —
    CLI commits therefore wire the registered ``write_file``/``patch``
    handlers in. A missing handler stays absent so the adapter blocks
    honestly instead of improvising a write path.
    """
    revision = services.store.get_revision(
        transaction.transaction_id, transaction.current_revision
    )
    needed = [
        node for node in revision.nodes if node.adapter_id == "workspace.v1"
    ]
    if not needed:
        return {}
    import tools.file_tools  # noqa: F401 — registers the file handlers
    from model_tools import registry as tool_registry

    invoke_map: dict = {}
    for node in needed:
        entry = tool_registry.get_entry(node.action)
        if entry is not None and entry.handler is not None:
            invoke_map[node.node_id] = entry.handler
    return invoke_map


def _cmd_commit(services: _Services, args) -> TransactionCommandResult:
    transaction = _require_transaction(services, args.transaction_id)
    result = services.coordinator.commit(
        args.transaction_id,
        through_node=args.through_node,
        requester=os.environ.get("USERNAME") or os.environ.get("USER") or "user",
        channel="cli",
        invoke_map=_workspace_invoke_map(services, transaction),
    )
    ok = result.status in {"committed", "ready"}
    output = (
        f"commit {result.status}; nodes: "
        f"{', '.join(result.committed_nodes) or 'none'}"
    )
    if result.blocked_node:
        output += f"; blocked at {result.blocked_node}"
    return TransactionCommandResult(EXIT_OK if ok else EXIT_ERROR, output, {
        "ok": ok, "action": "commit", "status": result.status,
        "committed_nodes": list(result.committed_nodes),
        "blocked_node": result.blocked_node,
    })


def _cmd_reconcile(services: _Services, args) -> TransactionCommandResult:
    _require_transaction(services, args.transaction_id)
    result = services.coordinator.reconcile(args.transaction_id)
    return TransactionCommandResult(EXIT_OK, (
        f"reconciled: status {result.status}; {dict(result.counts)}"
    ), {"ok": True, "action": "reconcile", "status": result.status,
        "counts": dict(result.counts)})


def _cmd_eligibility(services: _Services, args) -> TransactionCommandResult:
    from agent.effects.eligibility import eligibility_for_transaction

    _require_transaction(services, args.transaction_id)
    results = eligibility_for_transaction(
        services.store, services.adapters, args.transaction_id,
        cascade=args.cascade,
    )
    lines = []
    payload = {}
    for node_id, result in sorted(results.items()):
        # Blocker reasons are never abbreviated.
        lines.append(f"{node_id}: {result.code} — {result.reason}")
        if result.blockers:
            lines.append(f"  blockers: {', '.join(result.blockers)}")
        payload[node_id] = {
            "code": result.code,
            "can_execute": result.can_execute,
            "reason": result.reason,
            "fidelity": result.fidelity,
            "blockers": list(result.blockers),
            "required_cascade_node_ids": list(
                result.required_cascade_node_ids
            ),
        }
    return TransactionCommandResult(EXIT_OK, "\n".join(lines), {
        "ok": True, "action": "eligibility", "eligibility": payload,
    })


def _cmd_compensate(services: _Services, args) -> TransactionCommandResult:
    from agent.effects.models import EffectBlocked

    _require_transaction(services, args.transaction_id)
    try:
        outcome = services.coordinator.compensate(
            args.transaction_id, args.node_id, cascade=args.cascade,
        )
    except EffectBlocked as exc:
        return TransactionCommandResult(EXIT_ERROR, f"blocked: {exc}", {
            "ok": False, "action": "compensate", "error": str(exc),
        })
    output = (
        f"compensation {outcome.status}; nodes: "
        f"{', '.join(outcome.compensated_nodes) or 'none'}"
    )
    if outcome.error:
        output += f"\nstopped: {outcome.error}"
    return TransactionCommandResult(EXIT_OK, output, {
        "ok": outcome.status == "compensated",
        "action": "compensate", "status": outcome.status,
        "compensated_nodes": list(outcome.compensated_nodes),
        "error": outcome.error,
    })


def _cmd_receipt(services: _Services, args) -> TransactionCommandResult:
    transaction = _require_transaction(services, args.transaction_id)
    builder = services.receipt_builder()
    if args.recheck and transaction.receipt_id:
        observation = builder.recheck(transaction.receipt_id)
        return TransactionCommandResult(EXIT_OK, (
            f"recheck appended {observation.observation_id}: "
            f"{observation.status}"
        ), {"ok": True, "action": "receipt",
            "observation": {
                "observation_id": observation.observation_id,
                "status": observation.status,
            }})
    receipt = builder.issue(args.transaction_id)
    return TransactionCommandResult(EXIT_OK, (
        f"receipt {receipt.receipt_id}: {receipt.status} "
        f"({len(receipt.claims)} claims)"
    ), {"ok": True, "action": "receipt",
        "receipt": {
            "receipt_id": receipt.receipt_id,
            "status": receipt.status,
            "content_hash": receipt.content_hash,
        }})


def _cmd_outbox(services: _Services, args) -> TransactionCommandResult:
    outbox = services.outbox()
    sub = args.outbox_action
    if sub == "list":
        transaction = _require_transaction(services, args.transaction_id)
        rows = []
        for effect in services.store.list_effects(transaction.transaction_id):
            token = (effect.prepared or {}).get("prepared_token") or {}
            outbox_id = token.get("outbox_id")
            if not outbox_id:
                continue
            record = outbox.get_by_id(outbox_id)
            if record is not None:
                rows.append({
                    "outbox_id": record.outbox_id,
                    "status": record.status,
                    "revision": record.revision,
                    "not_before": record.not_before,
                    "content_hash": record.content_hash,
                })
        lines = [
            f"{row['outbox_id'][:24]:<26} {row['status']:<18} "
            f"rev {row['revision']}"
            for row in rows
        ]
        return TransactionCommandResult(
            EXIT_OK, "\n".join(lines) if rows else "no outbox rows",
            {"ok": True, "action": "outbox", "rows": rows},
        )
    if sub == "revise":
        current = outbox.get_by_id(args.outbox_id)
        if current is None:
            raise _usage(f"unknown outbox row {args.outbox_id!r}")
        revised = outbox.revise(
            args.outbox_id,
            content={"message": args.message},
            expected_revision=args.expected_revision,
            not_before=current.not_before,
        )
        if revised is None:
            return TransactionCommandResult(EXIT_ERROR, (
                "revision refused: row is past its revisable state"
            ), {"ok": False, "action": "outbox"})
        return TransactionCommandResult(EXIT_OK, (
            f"outbox revised to revision {revised.revision}"
        ), {"ok": True, "action": "outbox", "revision": revised.revision})
    if sub == "cancel":
        cancelled = outbox.cancel(args.outbox_id)
        return TransactionCommandResult(
            EXIT_OK if cancelled else EXIT_ERROR,
            "cancelled" if cancelled else "cancel refused (already "
            "dispatched or terminal)",
            {"ok": bool(cancelled), "action": "outbox"},
        )
    if sub == "release":
        from agent.effects.authority import (
            ApprovalIdentity,
            consume_bound_approval,
            request_bound_approval,
        )
        from agent.effects.models import content_hash
        from gateway.transaction_outbox import release_transaction_outbox

        row = outbox.get_by_id(args.outbox_id)
        if row is None:
            raise _usage(f"unknown outbox row {args.outbox_id!r}")
        if not (row.execution_id or "").startswith("tx:"):
            raise _usage(
                "outbox row is not owned by an action transaction"
            )
        transaction_id = row.execution_id[3:]
        transaction = _require_transaction(services, transaction_id)
        effect = services.store.latest_effects_by_node(transaction_id).get(
            row.node_id
        )
        if effect is None:
            raise _usage(
                f"outbox row {args.outbox_id!r} has no transaction effect"
            )
        requester = (
            os.environ.get("USERNAME") or os.environ.get("USER") or "user"
        )
        identity = ApprovalIdentity(
            transaction_id=transaction_id,
            revision=effect.revision,
            node_id=row.node_id,
            operation="release",
            args_hash=content_hash(dict(row.content or {})),
            preview_hash=row.content_hash,
            resources=(f"message:{row.platform}:{row.target}",),
            authority_version=transaction.authority_version,
            requester=requester,
            channel="cli",
        )
        # The dispatch boundary is irreversible: release consumes a
        # DURABLE exact approval. Missing binding → escalate through the
        # interactive human gate (fails closed with no human present);
        # session/permanent allowlisting never substitutes.
        consumption = consume_bound_approval(services.store, identity)
        if not consumption.approved:
            binding = request_bound_approval(
                services.store,
                transaction_id=transaction_id,
                revision=effect.revision,
                node_id=row.node_id,
                operation="release",
                args_hash=identity.args_hash,
                preview_hash=identity.preview_hash,
                resources=identity.resources,
                authority_version=transaction.authority_version,
                requester=requester,
                channel="cli",
                adapter_id="message-outbox.v1",
                action="send",
                reason=(
                    f"release delayed message to {row.platform}:{row.target} "
                    f"(revision {row.revision}); irreversible after dispatch"
                ),
                ttl_ms=5 * 60 * 1000,
            )
            if binding is not None:
                consumption = consume_bound_approval(
                    services.store, binding.identity(),
                )
        if not consumption.approved:
            return TransactionCommandResult(EXIT_ERROR, (
                "release refused: an exact consumed approval is required "
                f"(approval state: {consumption.code})"
            ), {"ok": False, "action": "outbox",
                "error": consumption.code})
        released = release_transaction_outbox(
            outbox, args.outbox_id, approval=consumption,
        )
        return TransactionCommandResult(
            EXIT_OK if released else EXIT_ERROR,
            "released for dispatch" if released else (
                "release refused: the row is not awaiting approval"
            ),
            {"ok": bool(released), "action": "outbox"},
        )
    raise _usage(f"unknown outbox action {sub!r}")


_HANDLERS = {
    "create": _cmd_create,
    "list": _cmd_list,
    "show": _cmd_show,
    "graph": _cmd_graph,
    "preview": _cmd_preview,
    "revise": _cmd_revise,
    "commit": _cmd_commit,
    "reconcile": _cmd_reconcile,
    "eligibility": _cmd_eligibility,
    "compensate": _cmd_compensate,
    "receipt": _cmd_receipt,
    "outbox": _cmd_outbox,
}

_COMMIT_GATED = frozenset({"commit", "compensate"})


# ── Parser ──────────────────────────────────────────────────────────────


def build_parser(parent_subparsers) -> argparse.ArgumentParser:
    parser = parent_subparsers.add_parser(
        "transaction",
        aliases=["tx"],
        help="Preview, revise, commit, reconcile, and compensate bounded actions",
        description=(
            "Reversible & revisable action transactions: preview a bounded "
            "action graph, commit each effect under freshly rechecked "
            "authority, revise pending work, and undo or compensate only "
            "what adapters truthfully support."
        ),
    )
    sub = parser.add_subparsers(dest="transaction_action")

    p = sub.add_parser("create", help="Create a transaction from plan YAML")
    p.add_argument("--plan", required=True)
    p.add_argument("--authority", required=True)
    p.add_argument("--transaction-id", dest="transaction_id")

    p = sub.add_parser("list", help="List transactions")
    p.add_argument("--status")

    p = sub.add_parser("show", help="Show one transaction")
    p.add_argument("transaction_id")

    p = sub.add_parser("graph", help="Show a revision graph")
    p.add_argument("transaction_id")
    p.add_argument("--revision", type=int)

    p = sub.add_parser("preview", help="Prepare and preview every node")
    p.add_argument("transaction_id")

    p = sub.add_parser("revise", help="Create a new revision (CAS)")
    p.add_argument("transaction_id")
    p.add_argument("--plan", required=True)
    p.add_argument("--expected-revision", dest="expected_revision",
                   type=int, required=True)
    p.add_argument("--reason", required=True)

    p = sub.add_parser("commit", help="Commit the ready revision")
    p.add_argument("transaction_id")
    p.add_argument("--through-node", dest="through_node")

    p = sub.add_parser("reconcile", help="Classify in-flight effects")
    p.add_argument("transaction_id")

    p = sub.add_parser("eligibility", help="Truthful undo eligibility")
    p.add_argument("transaction_id")
    p.add_argument("--cascade", action="store_true")

    p = sub.add_parser("compensate", help="Undo or compensate a node")
    p.add_argument("transaction_id")
    p.add_argument("node_id")
    p.add_argument("--cascade", action="store_true")

    p = sub.add_parser("receipt", help="Issue or recheck the receipt")
    p.add_argument("transaction_id")
    p.add_argument("--recheck", action="store_true")

    p_outbox = sub.add_parser("outbox", help="Delayed message outbox")
    outbox_sub = p_outbox.add_subparsers(dest="outbox_action")
    p = outbox_sub.add_parser("list", help="List outbox rows")
    p.add_argument("transaction_id")
    p = outbox_sub.add_parser("revise", help="Revise before release")
    p.add_argument("outbox_id")
    p.add_argument("--message", required=True)
    p.add_argument("--expected-revision", dest="expected_revision",
                   type=int, required=True)
    p = outbox_sub.add_parser("cancel", help="Cancel before dispatch")
    p.add_argument("outbox_id")
    p = outbox_sub.add_parser("release", help="Release for dispatch")
    p.add_argument("outbox_id")

    return parser


def _execute(args, *, output: Literal["text", "json"] = "text"):
    action = getattr(args, "transaction_action", None)
    if not action:
        raise _usage(
            "a transaction subcommand is required; see "
            "'hermes transaction --help'"
        )
    mode = _configured_mode()
    if mode == "off" and action not in _READ_ONLY:
        return TransactionCommandResult(EXIT_ERROR, (
            "action transactions are disabled (transactions.mode: off)"
        ), {"ok": False, "error": "transactions_disabled"})
    if mode == "preview" and action in _COMMIT_GATED:
        return TransactionCommandResult(EXIT_ERROR, (
            f"'{action}' requires transactions.mode: commit in config.yaml "
            "(current mode: preview). Preview, revise, reconcile, "
            "eligibility, and receipts remain available."
        ), {"ok": False, "error": "mode_gate", "mode": mode})
    if action == "outbox" and mode != "commit" and (
        getattr(args, "outbox_action", None) in {"release"}
    ):
        return TransactionCommandResult(EXIT_ERROR, (
            "outbox release requires transactions.mode: commit"
        ), {"ok": False, "error": "mode_gate", "mode": mode})

    services = _Services()
    try:
        result = _HANDLERS[action](services, args)
    except _CliUsageError as exc:
        return TransactionCommandResult(EXIT_VALIDATION, f"error: {exc}", {
            "ok": False, "error": str(exc), "code": "usage_error",
        })
    except Exception as exc:
        return TransactionCommandResult(EXIT_ERROR, f"error: {exc}", {
            "ok": False, "error": str(exc), "code": type(exc).__name__,
        })
    finally:
        services.close()
    if output == "json":
        return TransactionCommandResult(
            result.exit_code,
            json.dumps(result.payload, indent=2, sort_keys=True, default=str),
            result.payload,
        )
    return result


def _validate_argv(argv: Sequence[str]) -> list[str]:
    items = [str(item) for item in argv]
    if len(items) > _MAX_ARGS:
        raise _usage(
            f"too many arguments ({len(items)}); transaction commands "
            f"accept at most {_MAX_ARGS}"
        )
    total = sum(len(item.encode("utf-8")) for item in items)
    if total > _MAX_TOTAL_ARG_BYTES:
        raise _usage(
            "arguments exceed the 64 KiB total input bound for "
            "transaction commands"
        )
    return items


def run_argv(
    argv: Sequence[str], *, output: Literal["text", "json"] = "text"
) -> TransactionCommandResult:
    """The single shared surface behind CLI, slash, TUI RPC, and tests."""
    try:
        items = _validate_argv(argv)
    except _CliUsageError as exc:
        return TransactionCommandResult(EXIT_VALIDATION, str(exc), {
            "ok": False, "error": str(exc), "code": "usage_error",
        })
    wrap = _TransactionArgumentParser(prog="hermes", add_help=False)
    root_sub = wrap.add_subparsers(dest="_root")
    build_parser(root_sub)
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer), redirect_stderr(buffer):
            args = wrap.parse_args(["transaction", *items])
    except _CliUsageError as exc:
        return TransactionCommandResult(
            EXIT_VALIDATION,
            (buffer.getvalue() + f"error: {exc}").strip(),
            {"ok": False, "error": str(exc), "code": "usage_error"},
        )
    except SystemExit as exc:  # --help prints and exits 0
        code = exc.code if isinstance(exc.code, int) else EXIT_VALIDATION
        return TransactionCommandResult(
            code, buffer.getvalue().rstrip(), {"help": True},
        )
    try:
        return _execute(args, output=output)
    except _CliUsageError as exc:
        return TransactionCommandResult(EXIT_VALIDATION, f"error: {exc}", {
            "ok": False, "error": str(exc), "code": "usage_error",
        })


_SLASH_HELP = """/transaction — reversible & revisable action transactions
  create --plan p.yaml --authority a.yaml   plan a bounded action graph
  preview <tx>                              prepare + preview every node
  revise <tx> --plan p.yaml --expected-revision N --reason "..."
  commit <tx> [--through-node NODE]         commit under fresh authority
  reconcile <tx>                            classify in-flight effects
  eligibility <tx> [--cascade]              truthful undo eligibility
  compensate <tx> <node> [--cascade]        undo/compensate when eligible
  receipt <tx> [--recheck]                  issue/recheck immutable receipt
  outbox list <tx> | revise|cancel|release <outbox-id>"""


def run_slash(rest: str) -> str:
    """Execute a classic ``/transaction ...`` string."""
    try:
        tokens = shlex.split(rest, posix=os.name != "nt") if rest.strip() else []
    except ValueError:
        return "error: unbalanced quotes in /transaction arguments"
    if not tokens or tokens[0] in {"help", "--help", "-h", "?"}:
        return _SLASH_HELP
    return run_argv(tokens).output


def transaction_command(args: argparse.Namespace) -> int:
    """Entry point for ``hermes transaction ...`` argparse dispatch."""
    result = _execute(args)
    if result.output:
        print(result.output)
    return result.exit_code
