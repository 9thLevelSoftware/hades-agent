"""Stage 2 activation gate contracts that precede selection and provider I/O."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent.runtime_routing import (
    RUNTIME_ROUTING_CONTRACT_VERSION,
    AgentRuntimeContext,
    AgentRuntimePlan,
    AgentRuntimeRequest,
    AgentRuntimeSpec,
)
from plugins.auto_routing.auto_routing.runtime_resolver import (
    AutoRoutingRuntimeResolver,
)


def _request() -> AgentRuntimeRequest:
    baseline = AgentRuntimeSpec(
        model="baseline",
        provider="custom:test",
        base_url="https://baseline.invalid/v1",
        api_key="BASELINE_SECRET",
        resolution_state="requested",
        api_mode="chat_completions",
    )
    return AgentRuntimeRequest(
        contract_version=RUNTIME_ROUTING_CONTRACT_VERSION,
        context=AgentRuntimeContext(
            scope="fresh_session",
            task="ephemeral task",
            session_id="session-receipt-gate",
            task_id="task-receipt-gate",
            metadata={"platform": "test"},
        ),
        baseline=baseline,
    )


class _Backend:
    def __init__(self, *, receipt) -> None:
        self.receipt = receipt
        self.decide_calls = 0

    def read_binding(self, request):
        del request
        return None

    def load_config(self):
        return SimpleNamespace(
            activation=SimpleNamespace(mode="active"),
            scopes=SimpleNamespace(fresh_sessions=True, delegation=True),
        )

    def matching_activation_receipt(self, config):
        del config
        return self.receipt

    def decide(self, request, config, receipt):
        del config, receipt
        self.decide_calls += 1
        return AgentRuntimePlan(
            action="project",
            runtime=request.baseline,
            owns_fallbacks=True,
            reason_code="active_projected",
        )


def _resolver(tmp_path: Path, backend: _Backend) -> AutoRoutingRuntimeResolver:
    service = SimpleNamespace(hermes_home=tmp_path, close=lambda: None)
    return AutoRoutingRuntimeResolver(
        plugin_context=SimpleNamespace(),
        home_resolver=lambda: tmp_path,
        service_factory=lambda: service,
        backend_factory=lambda _service: backend,
    )


def test_hand_edited_active_without_matching_receipt_never_decides(
    tmp_path: Path,
) -> None:
    backend = _Backend(receipt=None)
    resolver = _resolver(tmp_path, backend)

    plan = resolver.resolve(_request())

    assert plan.action == "inherit"
    assert plan.reason_code == "activation_receipt_missing"
    assert backend.decide_calls == 0


def test_matching_receipt_is_checked_before_the_decision_boundary(
    tmp_path: Path,
) -> None:
    receipt = object()
    backend = _Backend(receipt=receipt)
    resolver = _resolver(tmp_path, backend)

    plan = resolver.resolve(_request())

    assert plan.action == "project"
    assert backend.decide_calls == 1
