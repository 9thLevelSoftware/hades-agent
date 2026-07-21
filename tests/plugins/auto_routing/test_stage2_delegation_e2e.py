"""Stage 2 delegation routing contracts.

These tests keep model-facing delegation authority unchanged while proving
that each child receives a durable construction identity and an exact runtime
before its ``AIAgent`` constructor runs.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from agent.runtime_routing import AgentRuntimePlan, AgentRuntimeSpec
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from tools import async_delegation as ad
from tools.delegate_tool import DELEGATE_TASK_SCHEMA, _build_child_agent, delegate_task


@pytest.fixture(autouse=True)
def _clean_async_state():
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


class _Resolver:
    def __init__(self, runtimes: dict[str, AgentRuntimeSpec]) -> None:
        self.runtimes = runtimes
        self.requests = []

    def requires_initial_task(self, scope: str) -> bool:
        return scope == "delegation"

    def resolve(self, request):
        self.requests.append(request)
        runtime = self.runtimes[request.context.task]
        return AgentRuntimePlan(
            action="project",
            runtime=runtime,
            decision_id=f"decision-{request.context.task_index}",
            bound_route_identity=f"route-{request.context.task_index}",
            owns_fallbacks=True,
            reason_code="active_projected",
        )

    def record_manual_pin(self, request) -> None:  # pragma: no cover - protocol
        del request

    def record_session_continuation(self, request) -> None:  # pragma: no cover
        del request

    def close(self) -> None:
        return None


def _install_resolver(monkeypatch, resolver: _Resolver) -> None:
    manager = PluginManager()
    manager._discovered = True
    context = PluginContext(
        PluginManifest(name="delegation-test-router", key="delegation-test-router"),
        manager,
    )
    context.register_agent_runtime_resolver(resolver)
    monkeypatch.setattr("hermes_cli.plugins._plugin_manager", manager)


def _runtime(model: str, pool: object) -> AgentRuntimeSpec:
    return AgentRuntimeSpec(
        model=model,
        provider="custom:selected",
        base_url="https://selected.invalid/v1",
        api_key="selected-secret",
        resolution_state="resolved",
        api_mode="chat_completions",
        credential_pool=pool,
        reasoning_config={"effort": "high"},
    )


def _parent() -> SimpleNamespace:
    return SimpleNamespace(
        base_url="https://baseline.invalid/v1",
        api_key="baseline-secret",
        provider="custom:baseline",
        api_mode="chat_completions",
        model="baseline-model",
        platform="cli",
        enabled_toolsets=["terminal"],
        providers_allowed=None,
        providers_ignored=None,
        providers_order=None,
        provider_sort=None,
        provider_require_parameters=False,
        provider_data_collection="",
        openrouter_min_coding_score=None,
        max_tokens=None,
        reasoning_config={"effort": "medium"},
        prefill_messages=None,
        request_overrides={},
        _fallback_chain=[{"provider": "custom:fallback", "model": "fallback"}],
        _credential_pool=None,
        _session_db=None,
        _delegate_depth=0,
        _active_children=[],
        _active_children_lock=threading.Lock(),
        _print_fn=None,
        tool_progress_callback=None,
        thinking_callback=None,
        session_id="parent-session",
        _current_turn_id="turn-1",
    )


class _Child(SimpleNamespace):
    def close(self) -> None:
        return None


def test_exact_runtime_is_passed_before_child_constructor(monkeypatch):
    selected_pool = object()
    resolver = _Resolver({"hard proof": _runtime("quality-model", selected_pool)})
    _install_resolver(monkeypatch, resolver)
    observed = []

    def constructor(**kwargs):
        observed.append(kwargs)
        return _Child(session_id=kwargs.get("session_id"))

    monkeypatch.setattr("run_agent.AIAgent", constructor)
    monkeypatch.setattr(
        "tools.delegate_tool._resolve_child_credential_pool",
        lambda *_args, **_kwargs: object(),
    )

    progress = []
    parent = _parent()
    parent.tool_progress_callback = (
        lambda event_type, tool_name=None, preview=None, args=None, **kwargs: progress.append(
            (event_type, kwargs)
        )
    )
    child = _build_child_agent(
        task_index=0,
        goal="hard proof",
        context=None,
        toolsets=None,
        model=None,
        max_iterations=20,
        task_count=1,
        parent_agent=parent,
        operation_id="deleg_exact",
        child_session_id="deleg_exact:0",
    )

    assert child.session_id == "deleg_exact:0"
    assert observed[0]["model"] == "quality-model"
    assert observed[0]["provider"] == "custom:selected"
    assert observed[0]["credential_pool"] is selected_pool
    assert observed[0]["reasoning_config"] == {"effort": "high"}
    assert observed[0]["fallback_model"] == []
    context = observed[0]["runtime_routing_context"]
    assert (context.operation_id, context.task_index) == ("deleg_exact", 0)
    assert resolver.requests[0].context.task == "hard proof"
    child._delegate_progress_callback("subagent.start", preview="hard proof")
    spawn = next(item for item in progress if item[0] == "subagent.start")
    assert spawn[1]["model"] == "quality-model"


def test_real_child_constructor_validates_the_sealed_selected_runtime(monkeypatch):
    from unittest.mock import patch

    from run_agent import AIAgent

    selected_pool = object()
    resolver = _Resolver({"hard proof": _runtime("quality-model", selected_pool)})
    _install_resolver(monkeypatch, resolver)

    class Client:
        def close(self) -> None:
            return None

    monkeypatch.setattr(
        AIAgent,
        "_create_openai_client",
        lambda *_args, **_kwargs: Client(),
    )
    monkeypatch.setattr(
        "tools.delegate_tool._resolve_child_credential_pool",
        lambda *_args, **_kwargs: object(),
    )

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
    ):
        child = _build_child_agent(
            task_index=0,
            goal="hard proof",
            context=None,
            toolsets=None,
            model=None,
            max_iterations=20,
            task_count=1,
            parent_agent=_parent(),
            operation_id="deleg_real",
            child_session_id="deleg_real:0",
        )

    try:
        assert child.model == "quality-model"
        assert child.provider == "custom:selected"
        assert child._credential_pool is selected_pool
        assert child._runtime_fallback_authority == "plugin"
        assert child._fallback_chain == []
        assert child._runtime_routing_binding.operation_id == "deleg_real"
    finally:
        child.close()


def test_batch_routes_each_goal_with_one_operation_and_ordered_task_indices(
    monkeypatch,
):
    pools = [object(), object()]
    resolver = _Resolver(
        {
            "easy summary": _runtime("fast-model", pools[0]),
            "hard coding proof": _runtime("quality-model", pools[1]),
        }
    )
    _install_resolver(monkeypatch, resolver)
    observed = []

    def constructor(**kwargs):
        observed.append(kwargs)
        return _Child(session_id=kwargs.get("session_id"))

    monkeypatch.setattr("run_agent.AIAgent", constructor)
    monkeypatch.setattr(
        "tools.delegate_tool._resolve_child_credential_pool",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        "tools.delegate_tool._run_single_child",
        lambda task_index, goal, child, parent_agent: {
            "task_index": task_index,
            "status": "completed",
            "summary": goal,
            "api_calls": 0,
            "duration_seconds": 0,
        },
    )
    monkeypatch.setattr(
        "tools.delegate_tool._load_config",
        lambda: {"max_iterations": 20},
    )
    monkeypatch.setattr(
        "tools.delegate_tool._resolve_delegation_credentials",
        lambda *_args, **_kwargs: {
            "model": None,
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
            "request_overrides": None,
            "max_output_tokens": None,
            "command": None,
            "args": None,
        },
    )

    result = json.loads(
        delegate_task(
            tasks=[{"goal": "easy summary"}, {"goal": "hard coding proof"}],
            parent_agent=_parent(),
            operation_id="deleg_batch",
        )
    )

    assert result["operation_id"] == "deleg_batch"
    assert [item["model"] for item in observed] == ["fast-model", "quality-model"]
    contexts = [item["runtime_routing_context"] for item in observed]
    assert [(item.operation_id, item.task_index) for item in contexts] == [
        ("deleg_batch", 0),
        ("deleg_batch", 1),
    ]
    assert [item.session_id for item in contexts] == [
        "deleg_batch:0",
        "deleg_batch:1",
    ]


@pytest.mark.parametrize("fixed_field", ["provider", "model"])
def test_any_fixed_delegation_runtime_field_bypasses_auto(
    monkeypatch, fixed_field: str
):
    selected_pool = object()
    resolver = _Resolver({"hard task": _runtime("auto-model", selected_pool)})
    _install_resolver(monkeypatch, resolver)
    captured = []

    def constructor(**kwargs):
        captured.append(kwargs)
        return _Child(session_id="fixed-child")

    monkeypatch.setattr("run_agent.AIAgent", constructor)
    config = {fixed_field: "fixed", "max_iterations": 20}
    monkeypatch.setattr("tools.delegate_tool._load_config", lambda: config)
    monkeypatch.setattr(
        "tools.delegate_tool._resolve_delegation_credentials",
        lambda *_args, **_kwargs: {
            "model": "fixed-model",
            "provider": "custom:fixed",
            "base_url": "https://fixed.invalid/v1",
            "api_key": "fixed-secret",
            "api_mode": "chat_completions",
            "request_overrides": None,
            "max_output_tokens": None,
            "command": None,
            "args": None,
        },
    )
    monkeypatch.setattr(
        "tools.delegate_tool._run_single_child",
        lambda task_index, goal, child, parent_agent: {
            "task_index": task_index,
            "status": "completed",
            "summary": goal,
            "api_calls": 0,
            "duration_seconds": 0,
        },
    )

    delegate_task(goal="hard task", parent_agent=_parent())

    assert resolver.requests == []
    assert "runtime_routing_context" not in captured[0]


def test_delegate_schema_exposes_no_runtime_routing_authority():
    properties = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
    task_properties = properties["tasks"]["items"]["properties"]
    forbidden = {
        "model",
        "provider",
        "reasoning",
        "reasoning_effort",
        "route_profile",
        "fallbacks",
    }
    assert forbidden.isdisjoint(properties)
    assert forbidden.isdisjoint(task_properties)
    assert {"goal", "tasks"}.issubset(properties)


def test_pending_route_returns_retryable_operation_without_constructing_child(
    monkeypatch,
):
    class PendingResolver(_Resolver):
        def resolve(self, request):
            self.requests.append(request)
            return AgentRuntimePlan(
                action="defer",
                runtime=request.baseline,
                owns_fallbacks=False,
                reason_code="operation_pending",
                retry_after_seconds=0.25,
            )

    resolver = PendingResolver({})
    _install_resolver(monkeypatch, resolver)
    constructed = []
    monkeypatch.setattr(
        "run_agent.AIAgent", lambda **kwargs: constructed.append(kwargs) or _Child()
    )
    monkeypatch.setattr(
        "tools.delegate_tool._load_config", lambda: {"max_iterations": 20}
    )
    monkeypatch.setattr(
        "tools.delegate_tool._resolve_delegation_credentials",
        lambda *_args, **_kwargs: {
            "model": None,
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
            "request_overrides": None,
            "max_output_tokens": None,
            "command": None,
            "args": None,
        },
    )

    result = json.loads(
        delegate_task(
            goal="hard task",
            parent_agent=_parent(),
            operation_id="deleg_pending",
        )
    )

    assert result == {
        "status": "deferred",
        "operation_id": "deleg_pending",
        "reason": "operation_pending",
        "retry_after_seconds": 0.25,
    }
    assert constructed == []


def test_background_reservation_precedes_route_and_child_construction(monkeypatch):
    events = []
    resolver = _Resolver({"hard task": _runtime("quality-model", object())})
    original_resolve = resolver.resolve

    def resolve(request):
        events.append("route")
        return original_resolve(request)

    resolver.resolve = resolve
    _install_resolver(monkeypatch, resolver)

    def constructor(**kwargs):
        events.append("construct")
        return _Child(session_id=kwargs.get("session_id"))

    def reserve(**kwargs):
        events.append("reserve")
        assert kwargs["delegation_id"] == "deleg_background"
        return {"status": "reserved", "delegation_id": kwargs["delegation_id"]}

    def dispatch(**kwargs):
        events.append("start")
        assert kwargs["delegation_id"] == "deleg_background"
        assert kwargs["pre_reserved"] is True
        return {"status": "dispatched", "delegation_id": kwargs["delegation_id"]}

    monkeypatch.setattr("run_agent.AIAgent", constructor)
    monkeypatch.setattr(ad, "reserve_async_delegation_batch", reserve)
    monkeypatch.setattr(
        ad,
        "dispatch_async_delegation_batch",
        dispatch,
    )
    monkeypatch.setattr(
        ad, "update_reserved_async_delegation_children", lambda *_args: True
    )
    monkeypatch.setattr("gateway.session_context.async_delivery_supported", lambda: True)
    monkeypatch.setattr(
        "tools.delegate_tool._load_config",
        lambda: {"max_iterations": 20},
    )
    monkeypatch.setattr(
        "tools.delegate_tool._resolve_delegation_credentials",
        lambda *_args, **_kwargs: {
            "model": None,
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
            "request_overrides": None,
            "max_output_tokens": None,
            "command": None,
            "args": None,
        },
    )

    result = json.loads(
        delegate_task(
            goal="hard task",
            background=True,
            parent_agent=_parent(),
            operation_id="deleg_background",
        )
    )

    assert result["operation_id"] == "deleg_background"
    assert events == ["reserve", "route", "construct", "start"]


def test_reserved_background_operation_recovers_unknown_without_running_child(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ad, "_records_path", lambda: tmp_path / "delegations.json")
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")
    started = []

    reserved = ad.reserve_async_delegation_batch(
        delegation_id="deleg_reserved",
        goals=["hard task"],
        context=None,
        toolsets=None,
        role="leaf",
        model=None,
        session_key="parent-session",
        parent_session_id="parent-session",
        child_session_ids=["deleg_reserved:0"],
        max_async_children=1,
    )
    assert reserved == {"status": "reserved", "delegation_id": "deleg_reserved"}

    with ad._DB_LOCK, ad._connect() as conn:
        conn.execute(
            "UPDATE async_delegations SET owner_pid=?, owner_started_at=NULL "
            "WHERE delegation_id=?",
            (99_999_999, "deleg_reserved"),
        )

    assert ad.recover_abandoned_delegations() == 1
    assert ad.get_durable_delegation("deleg_reserved")["state"] == "unknown"
    assert started == []


def test_failed_durable_reservation_never_leaks_capacity_or_starts_routing(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ad, "_records_path", lambda: tmp_path / "delegations.json")
    monkeypatch.setattr(
        ad,
        "_persist_dispatch",
        lambda _record: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    result = ad.reserve_async_delegation_batch(
        delegation_id="deleg_failed",
        goals=["hard task"],
        context=None,
        toolsets=None,
        role="leaf",
        model=None,
        session_key="parent-session",
        max_async_children=1,
    )

    assert result["status"] == "rejected"
    assert result["reason"] == "persistence"
    assert ad.active_count() == 0


def test_aborting_unstarted_reservation_terminalizes_operation_journal(
    tmp_path, monkeypatch
):
    from agent.operation_journal import OperationJournal
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "journal.db")
    journal = OperationJournal(db)
    ad._set_journal_for_tests(journal)
    monkeypatch.setattr(ad, "_records_path", lambda: tmp_path / "delegations.json")
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "async.db")

    reserved = ad.reserve_async_delegation_batch(
        delegation_id="deleg_abort",
        goals=["hard task"],
        context=None,
        toolsets=None,
        role="leaf",
        model=None,
        session_key="parent-session",
        max_async_children=1,
    )
    assert reserved["status"] == "reserved"
    assert journal.get("deleg_abort").state == "running"

    ad.abort_reserved_async_delegation("deleg_abort")

    assert journal.get("deleg_abort").state == "cancelled"
    db.close()
