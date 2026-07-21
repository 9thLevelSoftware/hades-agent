"""Adversarial Stage 3 evidence boundary and zero-adaptation proofs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import socket
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
import requests

from plugins.auto_routing.auto_routing.catalog import CatalogService
from plugins.auto_routing.auto_routing.classifier import StructuredTaskClassifier
from plugins.auto_routing.auto_routing.inventory import InventoryService
from plugins.auto_routing.auto_routing.selector import StaticSelector
from plugins.auto_routing.auto_routing.service import AutoRoutingService
from plugins.auto_routing.auto_routing.storage import (
    EVIDENCE_OBSERVER_BUSY_TIMEOUT_MS,
    ImmutableRecordConflict,
    RoutingStore,
)


CONTROL_PLANE_TABLES = (
    "authority_revisions",
    "adaptive_revisions",
    "active_adaptive_revisions",
    "adaptive_profile_revisions",
    "adaptive_profile_states",
    "adaptive_lifecycle_events",
    "adaptive_canary_assignments",
    "adaptive_optimizer_leases",
    "routing_decisions",
    "decision_candidates",
    "route_epochs",
    "decision_operations",
    "session_route_bindings",
    "activation_receipts",
)

POST_TURN_OUTCOME_KEYS = {
    "telemetry_schema_version",
    "session_id",
    "turn_id",
    "task_id",
    "observed_at_unix",
    "outcome",
    "api_calls",
    "tool_iterations",
    "retry_count",
    "cost_usd",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "reasoning_effort",
    "runtime_binding",
}

PUBLIC_BINDING_KEYS = {
    "scope",
    "session_id",
    "task_id",
    "action",
    "model",
    "provider",
    "decision_id",
}

FORBIDDEN_REPORT_KEYS = {
    "winner",
    "recommendation",
    "ranking",
    "rank",
    "score",
    "challenger",
    "canary",
    "promotion",
    "rollback",
}


def control_plane_fingerprint(connection) -> str:
    payload = []
    for table in CONTROL_PLANE_TABLES:
        columns = [
            str(row["name"])
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        ]
        order = ", ".join(f'"{column}"' for column in columns)
        rows = connection.execute(
            f'SELECT {order} FROM "{table}" ORDER BY {order}'
        ).fetchall()
        payload.append((table, [tuple(row) for row in rows]))
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _raise_if_called(name: str):
    def fail(*_args, **_kwargs):
        raise AssertionError(f"{name} must not run on a Stage 3 evidence path")

    return fail


def _recursive_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return {str(key) for key in value} | {
            nested
            for item in value.values()
            for nested in _recursive_keys(item)
        }
    if isinstance(value, (tuple, list)):
        return {nested for item in value for nested in _recursive_keys(item)}
    return set()


def _plugin_owned_artifacts(route) -> tuple[Path, ...]:
    candidates = set(route.auto_routing_artifacts())
    candidates.add(route.service.config_path)
    state_dir = route.home / "auto-routing"
    if state_dir.exists():
        candidates.update(path for path in state_dir.rglob("*") if path.is_file())
    candidates.update(route.home.glob("auto-routing-apply-*.pending.json"))
    candidates.update(route.home.glob("auto-routing-apply-*.journal.json"))
    return tuple(sorted(candidates))


def test_evidence_feedback_and_report_cannot_mutate_routing_authority(
    active_route,
    monkeypatch,
):
    before_db = control_plane_fingerprint(active_route.service.store.connection)
    before_config = active_route.service.config_path.read_bytes()
    forbidden_calls = (
        (StaticSelector, "select", "selector"),
        (StructuredTaskClassifier, "classify", "classifier"),
        (InventoryService, "refresh", "inventory refresh"),
        (CatalogService, "refresh", "catalog refresh"),
        (InventoryService, "apply_verification", "access verification"),
        (AutoRoutingService, "apply_activation", "activation apply"),
        (RoutingStore, "publish_revision", "adaptive publication"),
    )
    for owner, method, label in forbidden_calls:
        monkeypatch.setattr(owner, method, _raise_if_called(label))
    monkeypatch.setattr(
        active_route._adapter,
        "resolve",
        _raise_if_called("adapter resolution"),
    )

    committed = active_route.service.ingest_turn_outcome(active_route.payload())
    assert committed is not None
    active_route.service.record_feedback(
        evidence_id=committed.event.evidence_id,
        value="rating-1",
    )
    active_route.service.report(days=30)

    assert control_plane_fingerprint(active_route.service.store.connection) == before_db
    assert active_route.service.config_path.read_bytes() == before_config


def test_stage3_artifacts_exclude_raw_content_and_secrets(stage3_profile_factory):
    sentinels = (
        "RAW_TASK_STAGE3_SENTINEL",
        "RAW_RESPONSE_STAGE3_SENTINEL",
        "sk-stage3-secret-sentinel",
        "RAW_ENDPOINT_STAGE3_SENTINEL",
    )
    route = stage3_profile_factory(
        response_text=sentinels[1],
        selected_api_key=sentinels[2],
        endpoint_suffix=sentinels[3],
    )
    hook_payloads: list[dict[str, Any]] = []
    route._manager._hooks["post_turn_outcome"].append(
        lambda **payload: hook_payloads.append(payload)
    )

    result = route.run_real_turn(prompt=sentinels[0])
    assert result["final_response"].strip() == sentinels[1]
    report_json = json.dumps(route.service.report(days=30), sort_keys=True)
    explain_json = json.dumps(
        route.service.explain(
            decision_id=result["decision_id"],
            detailed=True,
        ),
        sort_keys=True,
    )
    rendered = b"\n".join(
        path.read_bytes() for path in _plugin_owned_artifacts(route) if path.exists()
    ) + report_json.encode() + explain_json.encode()
    rows = route.service.store.connection.execute(
        "SELECT * FROM evidence_events ORDER BY evidence_id"
    ).fetchall()
    assert rows
    stored = json.dumps(
        [{key: row[key] for key in row.keys()} for row in rows],
        sort_keys=True,
        default=str,
    ).encode()
    assert hook_payloads
    assert all(set(payload) == POST_TURN_OUTCOME_KEYS for payload in hook_payloads)
    assert all(
        payload["runtime_binding"] is None
        or set(payload["runtime_binding"]) == PUBLIC_BINDING_KEYS
        for payload in hook_payloads
    )
    hook_json = json.dumps(hook_payloads, sort_keys=True).encode()
    for sentinel in sentinels:
        encoded = sentinel.encode()
        assert encoded not in rendered
        assert encoded not in stored
        assert encoded not in hook_json


def test_duplicate_hook_delivery_is_one_row_under_contention(active_route):
    payload = active_route.payload()

    def ingest_once(_index):
        service = active_route.fresh_service(allow_cross_thread_close=True)
        try:
            return service.ingest_turn_outcome(payload)
        finally:
            service.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(ingest_once, range(24)))

    assert sum(result.status == "inserted" for result in results) == 1
    assert sum(result.status == "replayed" for result in results) == 23
    assert active_route.service.store.count_evidence_events() == 1


def test_exact_hook_replay_after_restart_is_one_immutable_row(active_route):
    payload = active_route.payload()
    first = active_route.service.ingest_turn_outcome(payload)
    restarted = active_route.fresh_service()

    replay = restarted.ingest_turn_outcome(payload)

    assert first.status == "inserted"
    assert replay.status == "replayed"
    assert replay.event == first.event
    assert restarted.store.count_evidence_events() == 1


def test_same_evidence_id_with_different_document_is_rejected(active_route):
    first_payload = active_route.payload()
    first = active_route.service.ingest_turn_outcome(first_payload)
    changed_payload = dict(first_payload)
    changed_payload["cost_usd"] = first_payload["cost_usd"] + 1.0

    with pytest.raises(ImmutableRecordConflict):
        active_route.service.ingest_turn_outcome(changed_payload)

    assert active_route.service.store.count_evidence_events() == 1
    assert active_route.service.store.read_evidence_event(
        first.event.evidence_id
    ) == first.event


def test_locked_evidence_store_never_loses_user_response(active_route):
    callback_durations: list[float] = []
    callbacks = active_route._manager._hooks["post_turn_outcome"]
    callback_index = next(
        index
        for index, callback in enumerate(callbacks)
        if callback == active_route.resolver.on_post_turn_outcome
    )

    def timed_observer(**payload):
        started = time.monotonic()
        try:
            return active_route.resolver.on_post_turn_outcome(**payload)
        finally:
            callback_durations.append(time.monotonic() - started)

    callbacks[callback_index] = timed_observer
    lock_connection = sqlite3.connect(
        active_route.service.store.path,
        timeout=0.05,
        check_same_thread=False,
    )
    lock_connection.execute("PRAGMA busy_timeout = 50")

    def lock_after_pre_api_request() -> None:
        if not lock_connection.in_transaction:
            lock_connection.execute("BEGIN IMMEDIATE")

    try:
        result = active_route.run_real_turn(
            prompt="finish normally",
            on_chat_request_entry=lock_after_pre_api_request,
        )
    finally:
        if lock_connection.in_transaction:
            lock_connection.execute("ROLLBACK")
        lock_connection.close()

    assert result["final_response"].strip() == "ok"
    assert result["messages"][-1]["role"] == "assistant"
    assert len(callback_durations) == 1
    assert callback_durations[0] < (
        EVIDENCE_OBSERVER_BUSY_TIMEOUT_MS / 1000.0 + 0.75
    )


def test_corrupt_evidence_table_never_loses_user_response(active_route):
    active_route.service.store.connection.execute("DROP TABLE evidence_events")
    active_route.service.store.connection.commit()

    result = active_route.run_real_turn(prompt="finish despite corrupt evidence state")

    assert result["final_response"].strip() == "ok"
    assert result["messages"][-1]["role"] == "assistant"


def test_static_selector_and_adaptive_baseline_do_not_import_local_evidence():
    repo_root = Path(__file__).resolve().parents[3]
    forbidden_consumers = (
        repo_root / "plugins/auto_routing/auto_routing/selector.py",
        repo_root / "plugins/auto_routing/auto_routing/scoring.py",
        repo_root / "plugins/auto_routing/auto_routing/classifier.py",
        repo_root / "plugins/auto_routing/auto_routing/catalog.py",
    )
    scoring = forbidden_consumers[1]
    concrete_consumers = forbidden_consumers
    if not scoring.exists():
        assert not scoring.exists()
        concrete_consumers = tuple(
            path for path in forbidden_consumers if path != scoring
        )
    for path in concrete_consumers:
        assert path.exists()
        source = path.read_text(encoding="utf-8")
        assert "EvidenceEvent" not in source
        assert "evidence_events" not in source
        assert "from .evidence" not in source


def test_report_contains_no_ranking_or_adaptation_directives(active_route):
    event = active_route.service.ingest_turn_outcome(active_route.payload()).event
    active_route.service.record_feedback(
        evidence_id=event.evidence_id,
        value="rating-5",
    )

    report = active_route.service.report(days=30)

    assert _recursive_keys(report).isdisjoint(FORBIDDEN_REPORT_KEYS)


def test_evidence_feedback_and_report_make_no_network_calls(
    active_route,
    monkeypatch,
):
    payload = active_route.payload()
    payload["turn_id"] = hashlib.sha256(
        b"network-gated-stage3-fresh-insert"
    ).hexdigest()
    network_forbidden = _raise_if_called("network")
    monkeypatch.setattr(socket, "create_connection", network_forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", network_forbidden)
    monkeypatch.setattr(requests.Session, "request", network_forbidden)
    monkeypatch.setattr(httpx.Client, "request", network_forbidden)

    committed = active_route.service.ingest_turn_outcome(payload)
    assert committed is not None
    assert committed.status == "inserted"
    active_route.service.record_feedback(
        evidence_id=committed.event.evidence_id,
        value="corrected",
    )
    active_route.service.report(days=30)
