"""Contracts for the generic, plugin-owned agent runtime resolver seam."""

from __future__ import annotations

from dataclasses import replace
import json
import threading
from types import SimpleNamespace
from collections.abc import Mapping
from unittest.mock import patch

import pytest

from agent.runtime_routing import (
    RUNTIME_ROUTING_CONTRACT_VERSION,
    AgentRuntimeContext,
    AgentRuntimePlan,
    AgentRuntimeRequest,
    AgentRuntimeSpec,
    InvalidPreparedAgentRuntime,
    RuntimeContinuationError,
    RuntimeRoutingBinding,
    RuntimeRoutingDeferred,
    apply_runtime_plan_to_constructor_arguments,
    apply_manual_runtime_transition,
    failed_runtime_spec_from,
    finalize_prepared_agent_runtime,
    prepare_agent_runtime,
    public_runtime_binding,
    record_runtime_session_continuation,
    repair_runtime_session_continuations,
    resolve_ordinary_hermes_runtime,
    runtime_resolver_requires_initial_task,
    validate_prepared_agent_runtime,
)
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from run_agent import AIAgent


class FakeResolver:
    def __init__(self, plan=None, *, requires_task=False, error=None):
        self.plan = plan
        self.requires_task = requires_task
        self.error = error
        self.requests = []
        self.manual_pins = []
        self.continuations = []
        self.closed = 0

    def requires_initial_task(self, scope):
        return self.requires_task

    def resolve(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if callable(self.plan):
            return self.plan(request)
        if self.plan is not None:
            return self.plan
        return AgentRuntimePlan(
            action="inherit",
            runtime=request.baseline,
            reason_code="baseline_inherit",
        )

    def record_manual_pin(self, request):
        self.manual_pins.append(request)

    def record_session_continuation(self, request):
        self.continuations.append(request)

    def close(self):
        self.closed += 1


class HostileMapping(Mapping):
    def __getitem__(self, _key):
        raise RuntimeError("HOSTILE_MAPPING_SECRET")

    def __iter__(self):
        raise RuntimeError("HOSTILE_MAPPING_SECRET")

    def __len__(self):
        raise RuntimeError("HOSTILE_MAPPING_SECRET")


@pytest.mark.parametrize(
    ("record_model", "provider_default", "expected"),
    [
        ("record-model", "unused-default", "record-model"),
        (None, "provider-default", "provider-default"),
    ],
)
def test_ordinary_runtime_uses_provider_model_when_baseline_model_is_empty(
    monkeypatch,
    record_model,
    provider_default,
    expected,
):
    baseline = _spec(model="", provider="openrouter", base_url="", api_key=None)
    record = {
        "provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "resolved-key",
        "api_mode": "chat_completions",
    }
    if record_model is not None:
        record["model"] = record_model
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: record,
    )
    monkeypatch.setattr(
        "hermes_cli.models.get_default_model_for_provider",
        lambda provider: provider_default
        if provider == "openrouter"
        else pytest.fail("unexpected provider"),
    )

    resolved = resolve_ordinary_hermes_runtime(
        baseline,
        owns_fallbacks=False,
    ).runtime

    assert resolved.model == expected


def _install_resolver(monkeypatch, resolver):
    manager = PluginManager()
    manager._discovered = True
    context = PluginContext(PluginManifest(name="test-router", key="test-router"), manager)
    context.register_agent_runtime_resolver(resolver)
    monkeypatch.setattr("hermes_cli.plugins._plugin_manager", manager)
    return manager


def _spec(
    model="baseline-model",
    provider="custom:baseline",
    *,
    base_url="https://baseline.invalid/v1",
    api_key="baseline-secret",
    resolution_state="resolved",
    api_mode="chat_completions",
    **kwargs,
):
    return AgentRuntimeSpec(
        model=model,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        resolution_state=resolution_state,
        api_mode=api_mode,
        **kwargs,
    )


def _context(
    task="first task",
    *,
    session_id="session-a",
    task_id="task-a",
    operation_id=None,
    is_resume=False,
    manual_runtime_pin=False,
):
    return AgentRuntimeContext(
        scope="fresh_session",
        task=task,
        session_id=session_id,
        task_id=task_id,
        operation_id=operation_id,
        is_resume=is_resume,
        manual_runtime_pin=manual_runtime_pin,
        metadata={"domains": ["coding"], "complexity": 0.75, "image_count": 1},
    )


def _request(*, context=None, baseline=None, version=RUNTIME_ROUTING_CONTRACT_VERSION):
    return AgentRuntimeRequest(
        contract_version=version,
        context=context or _context(),
        baseline=baseline or _spec(),
    )


def _projected_spec(**overrides):
    values = {
        "model": "selected-model",
        "provider": "custom:selected",
        "base_url": "https://selected.invalid/v1",
        "api_key": "selected-secret",
        "resolution_state": "resolved",
        "api_mode": "chat_completions",
        "acp_command": "selected-acp",
        "acp_args": ("--token", "ACP_TOKEN_SENTINEL"),
        "credential_pool": object(),
        "reasoning_config": {"effort": "high"},
        "fallback_model": (
            {
                "provider": "custom:plugin-fallback",
                "model": "plugin-fallback",
                "base_url": "https://fallback.invalid/v1",
                "api_key": "FALLBACK_KEY_SENTINEL",
            },
        ),
    }
    values.update(overrides)
    return AgentRuntimeSpec(**values)


def _project_plan(spec=None, **overrides):
    values = {
        "action": "project",
        "runtime": spec or _projected_spec(),
        "decision_id": "decision-a",
        "bound_route_identity": "route-a",
        "owns_fallbacks": True,
        "reason_code": "active_projected",
        "event": {"profile_id": "balanced", "candidate_count": 2},
    }
    values.update(overrides)
    return AgentRuntimePlan(**values)


def _finalized(request, plan=None):
    prepared = prepare_agent_runtime(request)
    effective = plan.runtime if plan is not None else prepared.plan.runtime
    return finalize_prepared_agent_runtime(prepared, request, effective)


def test_runtime_records_deep_freeze_public_mappings_and_never_repr_secrets():
    request = _request(
        context=_context(
            task=[
                {"type": "text", "text": "TASK_PAYLOAD_SENTINEL"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ]
        ),
        baseline=_spec(
            base_url="https://user:pass@example.invalid/v1",
            api_key="API_KEY_SENTINEL",
            acp_args=("--token", "ACP_TOKEN_SENTINEL"),
            credential_pool={"token": "POOL_TOKEN_SENTINEL"},
            reasoning_config={"effort": "high"},
            fallback_model=({"api_key": "FALLBACK_KEY_SENTINEL"},),
        ),
    )

    with pytest.raises(TypeError):
        request.context.metadata["complexity"] = 0.1
    with pytest.raises(TypeError):
        request.baseline.reasoning_config["effort"] = "low"
    with pytest.raises(TypeError):
        request.baseline.fallback_model[0]["api_key"] = "replacement"

    rendered = repr(request)
    for secret in (
        "TASK_PAYLOAD_SENTINEL",
        "API_KEY_SENTINEL",
        "user:pass",
        "ACP_TOKEN_SENTINEL",
        "POOL_TOKEN_SENTINEL",
        "FALLBACK_KEY_SENTINEL",
    ):
        assert secret not in rendered


def test_context_rejects_content_or_location_bearing_public_metadata_without_echo():
    secret = "https://user:pass@example.invalid/private"
    with pytest.raises(ValueError, match="metadata") as exc:
        replace(_context(), metadata={"source_url": secret})
    assert secret not in str(exc.value)


def test_context_hostile_metadata_raises_only_finite_validation_error():
    with pytest.raises(ValueError, match="metadata") as exc:
        replace(_context(), metadata=HostileMapping())
    assert "HOSTILE_MAPPING_SECRET" not in str(exc.value)


def test_resolution_failure_uses_finite_code_and_never_exposes_raw_exception():
    error = RuntimeError("https://user:pass@example.invalid?token=EXCEPTION_SECRET")
    spec, public_event = failed_runtime_spec_from(error, model="m", provider="p")

    assert spec.resolution_state == "failed"
    assert spec.resolution_reason_code == "resolution_failed"
    assert public_event == {"reason_code": "resolution_failed"}
    assert "EXCEPTION_SECRET" not in repr(spec)
    assert "EXCEPTION_SECRET" not in repr(public_event)
    assert "user:pass" not in repr(public_event)


@pytest.mark.parametrize(
    "bad_plan",
    [
        AgentRuntimePlan(
            action="inherit",
            runtime=_spec(),
            reason_code="https://user:pass.invalid?token=PLAN_SECRET",
        ),
        AgentRuntimePlan(
            action="inherit",
            runtime=_spec(),
            reason_code="baseline_inherit",
            event={"api_key": "EVENT_SECRET"},
        ),
        SimpleNamespace(action="explode", runtime=_spec(), reason_code="baseline_inherit"),
    ],
)
def test_malformed_or_secret_bearing_plan_fails_open_without_echo(
    monkeypatch, caplog, bad_plan
):
    _install_resolver(monkeypatch, FakeResolver(plan=bad_plan))
    caplog.set_level("WARNING")

    request = _request()
    prepared = prepare_agent_runtime(request)

    assert prepared.plan.action == "inherit"
    assert prepared.plan.runtime == _spec()
    assert prepared.plan.reason_code == "resolver_contract_invalid"
    finalized = finalize_prepared_agent_runtime(prepared, request, request.baseline)
    rendered = repr(prepared) + repr(public_runtime_binding(finalized)) + caplog.text
    assert "PLAN_SECRET" not in rendered
    assert "EVENT_SECRET" not in rendered
    assert "user:pass" not in rendered


@pytest.mark.parametrize(
    "event",
    [
        ["safe-but-not-a-mapping"],
        {"apiKey": "safe-looking-value"},
        {"label": "ghp_0123456789ABCDEF"},
        {"location": "/home/alice/private"},
        {"location": "../private/file"},
        {"location": "private/file"},
        {"location": "C:private"},
        {"location": "C:\\private\\file"},
        {"location": "."},
        {"location": ".."},
        {"location": "file:/etc/passwd"},
        {"filename": "PUBLIC_FILENAME_SECRET.txt"},
        {"filepath": "PUBLIC_FILEPATH_SECRET.txt"},
        {"location": "PUBLIC_LOCATION_SECRET.txt"},
        {"authentication": "PUBLIC_AUTH_SECRET"},
        {"payloadData": "safe-looking-value"},
        {"label": "sk-proj-0123456789ABCDEF"},
        {"label": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature"},
        {"ghp_0123456789ABCDEF": 1},
        {"not a fact key": 1},
        {"count": 2**60},
        {"ratio": 1e300},
    ],
)
def test_event_rejects_non_mapping_normalized_secret_keys_tokens_and_paths(
    monkeypatch, caplog, event
):
    caplog.set_level("WARNING")
    plan = AgentRuntimePlan(
        action="inherit",
        runtime=_spec(),
        reason_code="baseline_inherit",
        event=event,
    )
    _install_resolver(monkeypatch, FakeResolver(plan=plan))

    prepared = prepare_agent_runtime(_request())

    assert prepared.plan.reason_code == "resolver_contract_invalid"
    assert "ghp_0123456789ABCDEF" not in caplog.text
    assert "/home/alice/private" not in caplog.text
    assert "../private/file" not in caplog.text
    assert "PUBLIC_FILENAME_SECRET" not in caplog.text
    assert "PUBLIC_FILEPATH_SECRET" not in caplog.text
    assert "PUBLIC_LOCATION_SECRET" not in caplog.text
    assert "PUBLIC_AUTH_SECRET" not in caplog.text


def test_event_accepts_bounded_content_free_facts_without_substring_false_positives(
    monkeypatch,
):
    event = {
        "domains": ["coding"],
        "complexity": 0.75,
        "profile_id": "balanced",
        "candidate_count": 2,
        "secretary_mode": "enabled",
        "contents_count": 3,
    }
    plan = AgentRuntimePlan(
        action="inherit",
        runtime=_spec(),
        reason_code="baseline_inherit",
        event=event,
    )
    _install_resolver(monkeypatch, FakeResolver(plan=plan))

    prepared = prepare_agent_runtime(_request())

    assert prepared.plan.reason_code == "baseline_inherit"
    assert dict(prepared.plan.event)["secretary_mode"] == "enabled"


def test_hostile_event_mapping_fails_finitely_without_echo(monkeypatch, caplog):
    caplog.set_level("WARNING")
    _install_resolver(
        monkeypatch,
        FakeResolver(
            plan=lambda _request: AgentRuntimePlan(
                action="inherit",
                runtime=_spec(),
                reason_code="baseline_inherit",
                event=HostileMapping(),
            )
        ),
    )

    prepared = prepare_agent_runtime(_request())

    assert prepared.plan.reason_code == "resolver_contract_invalid"
    assert "HOSTILE_MAPPING_SECRET" not in caplog.text


@pytest.mark.parametrize("reason", [None, 7, object()])
def test_non_string_plan_reason_fails_open(monkeypatch, reason):
    plan = AgentRuntimePlan(
        action="inherit",
        runtime=_spec(),
        reason_code=reason,
    )
    _install_resolver(monkeypatch, FakeResolver(plan=plan))

    prepared = prepare_agent_runtime(_request())

    assert prepared.plan.reason_code == "resolver_contract_invalid"


def test_unresolved_project_fails_open(monkeypatch):
    unresolved = _projected_spec(
        resolution_state="requested", resolution_reason_code="unresolved"
    )
    _install_resolver(monkeypatch, FakeResolver(plan=_project_plan(unresolved)))

    prepared = prepare_agent_runtime(_request())

    assert prepared.plan.action == "inherit"
    assert prepared.plan.reason_code == "resolver_contract_invalid"


def test_resolved_project_without_exact_execution_binding_fails_open(monkeypatch):
    non_executable = _projected_spec(
        base_url="",
        api_key=None,
        credential_pool=None,
        acp_command=None,
        acp_args=(),
        fallback_model=(),
    )
    _install_resolver(
        monkeypatch, FakeResolver(plan=_project_plan(non_executable))
    )

    prepared = prepare_agent_runtime(_request())

    assert prepared.plan.action == "inherit"
    assert prepared.plan.runtime == _spec()
    assert prepared.plan.reason_code == "resolver_contract_invalid"


@pytest.mark.parametrize(
    "runtime",
    [
        _projected_spec(fallback_model=()),
    ],
)
def test_project_accepts_only_approved_exact_execution_binding_forms(
    monkeypatch, runtime
):
    _install_resolver(monkeypatch, FakeResolver(plan=_project_plan(runtime)))

    prepared = prepare_agent_runtime(_request())

    assert prepared.plan.action == "project"
    assert prepared.plan.runtime is runtime


@pytest.mark.parametrize(
    "runtime",
    [
        _projected_spec(
            base_url="",
            api_key=None,
            credential_pool=object(),
            acp_command=None,
            acp_args=(),
            fallback_model=(),
        ),
        _projected_spec(
            provider="copilot-acp",
            base_url="",
            api_key=None,
            credential_pool=None,
            acp_command="copilot",
            acp_args=("--stdio",),
            fallback_model=(),
        ),
        _projected_spec(
            provider="moa",
            base_url="",
            api_key=None,
            credential_pool=None,
            acp_command=None,
            acp_args=(),
            fallback_model=(),
        ),
        _projected_spec(
            provider="bedrock",
            base_url="",
            api_key=None,
            api_mode="bedrock_converse",
            credential_pool=None,
            acp_command=None,
            acp_args=(),
            fallback_model=(),
        ),
        _projected_spec(api_mode="", fallback_model=()),
        _projected_spec(provider=" Custom:Selected ", fallback_model=()),
    ],
)
def test_project_rejects_non_executable_or_noncanonical_runtime_forms(
    monkeypatch, runtime
):
    _install_resolver(monkeypatch, FakeResolver(plan=_project_plan(runtime)))

    prepared = prepare_agent_runtime(_request())

    assert prepared.plan.action == "inherit"
    assert prepared.plan.reason_code == "resolver_contract_invalid"


@pytest.mark.parametrize(
    "runtime",
    [
        _projected_spec(
            model="amazon.nova-pro-v1:0",
            provider="bedrock",
            base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
            api_key="NOT_AWS_CANONICAL",
            api_mode="bedrock_converse",
            acp_command=None,
            acp_args=(),
            credential_pool=None,
            fallback_model=(),
        ),
        _projected_spec(
            model="amazon.nova-pro-v1:0",
            provider="bedrock",
            base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
            api_key="aws-sdk",
            api_mode="anthropic_messages",
            acp_command=None,
            acp_args=(),
            credential_pool=None,
            fallback_model=(),
        ),
        _projected_spec(
            model="anthropic.claude-sonnet-4-20250514-v1:0",
            provider="bedrock",
            base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
            api_key="aws-sdk",
            api_mode="bedrock_converse",
            acp_command=None,
            acp_args=(),
            credential_pool=None,
            fallback_model=(),
        ),
        _projected_spec(
            model="amazon.nova-pro-v1:0",
            provider="bedrock",
            base_url="https://proxy.invalid/v1",
            api_key="aws-sdk",
            api_mode="bedrock_converse",
            acp_command=None,
            acp_args=(),
            credential_pool=None,
            fallback_model=(),
        ),
    ],
    ids=["key", "nova-mode", "claude-mode", "endpoint"],
)
def test_project_rejects_noncanonical_bedrock_runtime(monkeypatch, runtime):
    _install_resolver(monkeypatch, FakeResolver(plan=_project_plan(runtime)))

    prepared = prepare_agent_runtime(_request())

    assert prepared.plan.action == "inherit"
    assert prepared.plan.reason_code == "resolver_contract_invalid"


def test_project_rejects_malformed_owned_fallback_before_sealing(monkeypatch):
    selected = _projected_spec(
        fallback_model=(
            {"provider": "custom:valid", "model": "valid-model"},
            {"provider": "custom:missing-model"},
        )
    )
    _install_resolver(monkeypatch, FakeResolver(plan=_project_plan(selected)))

    prepared = prepare_agent_runtime(_request())

    assert prepared.plan.action == "inherit"
    assert prepared.plan.reason_code == "resolver_contract_invalid"


@pytest.mark.parametrize(
    "runtime",
    [
        _projected_spec(api_mode="bedrock_converse", fallback_model=()),
        _projected_spec(api_mode="codex_app_server", fallback_model=()),
        _projected_spec(
            provider="copilot-acp",
            api_mode="bedrock_converse",
            acp_command="copilot",
            acp_args=("--stdio",),
            fallback_model=(),
        ),
        _projected_spec(base_url=" ", fallback_model=()),
        _projected_spec(base_url="not-a-url", fallback_model=()),
        _projected_spec(base_url="ftp://selected.invalid/v1", fallback_model=()),
        _projected_spec(
            base_url="https://user:pass@selected.invalid/v1", fallback_model=()
        ),
        _projected_spec(
            base_url="https://selected.invalid/v1#fragment", fallback_model=()
        ),
        _projected_spec(
            base_url="https://selected.invalid/v1?tag=a&tag=b", fallback_model=()
        ),
        _projected_spec(
            base_url="https://selected.invalid/v1?flag=", fallback_model=()
        ),
        _projected_spec(
            provider="minimax-oauth",
            base_url="https://api.minimax.invalid/anthropic",
            api_key="short-lived-token",
            api_mode="anthropic_messages",
            acp_command=None,
            acp_args=(),
            credential_pool=None,
            fallback_model=(),
        ),
        _projected_spec(
            provider="copilot-acp",
            base_url="acp://copilot",
            api_key="copilot-acp",
            api_mode="chat_completions",
            acp_command=" ",
            acp_args=("--stdio",),
            credential_pool=None,
            fallback_model=(),
        ),
        _projected_spec(
            provider="copilot-acp",
            base_url="acp://copilot",
            api_key="copilot-acp",
            api_mode="chat_completions",
            acp_command="copilot",
            acp_args=(" ",),
            credential_pool=None,
            fallback_model=(),
        ),
    ],
    ids=[
        "custom-bedrock-mode",
        "custom-app-server-mode",
        "acp-bedrock-mode",
        "blank-url",
        "relative-url",
        "ftp-url",
        "userinfo-url",
        "fragment-url",
        "duplicate-query",
        "blank-query-value",
        "minimax-static-token",
        "acp-blank-command",
        "acp-blank-arg",
    ],
)
def test_project_rejects_incompatible_mode_endpoint_or_adapter_form(
    monkeypatch, runtime
):
    _install_resolver(monkeypatch, FakeResolver(plan=_project_plan(runtime)))

    prepared = prepare_agent_runtime(_request())

    assert prepared.plan.action == "inherit"
    assert prepared.plan.reason_code == "resolver_contract_invalid"


@pytest.mark.parametrize(
    "bad_fallback",
    [
        {"provider": 7, "model": "numeric-provider"},
        {"provider": " auto ", "model": "global-auto"},
        {"provider": " Custom:Mixed ", "model": " mixed-model "},
    ],
)
def test_project_rejects_noncanonical_owned_fallback(monkeypatch, bad_fallback):
    selected = _projected_spec(fallback_model=(bad_fallback,))
    _install_resolver(monkeypatch, FakeResolver(plan=_project_plan(selected)))

    prepared = prepare_agent_runtime(_request())

    assert prepared.plan.action == "inherit"
    assert prepared.plan.reason_code == "resolver_contract_invalid"


def test_resolver_exception_fails_open_with_only_finite_public_reason(monkeypatch, caplog):
    secret = "https://user:pass.invalid?token=RESOLVER_EXCEPTION_SECRET"
    _install_resolver(monkeypatch, FakeResolver(error=RuntimeError(secret)))
    caplog.set_level("WARNING")

    prepared = prepare_agent_runtime(_request())

    assert prepared.plan.action == "inherit"
    assert prepared.plan.reason_code == "resolver_error"
    assert secret not in caplog.text
    assert "RESOLVER_EXCEPTION_SECRET" not in repr(prepared)


def test_wrong_contract_version_fails_open_without_invoking_resolver(monkeypatch):
    resolver = FakeResolver()
    _install_resolver(monkeypatch, resolver)

    prepared = prepare_agent_runtime(_request(version=999))

    assert prepared.plan.action == "inherit"
    assert prepared.plan.reason_code == "resolver_contract_invalid"
    assert resolver.requests == []


def test_absent_resolver_returns_exact_baseline(monkeypatch):
    manager = PluginManager()
    manager._discovered = True
    monkeypatch.setattr("hermes_cli.plugins._plugin_manager", manager)
    baseline = _spec(credential_pool=object())

    prepared = prepare_agent_runtime(_request(baseline=baseline))

    assert prepared.plan.action == "inherit"
    assert prepared.plan.reason_code == "resolver_absent"
    assert prepared.plan.runtime is baseline


def test_host_can_query_whether_construction_requires_first_task(monkeypatch):
    resolver = FakeResolver(requires_task=True)
    _install_resolver(monkeypatch, resolver)

    assert runtime_resolver_requires_initial_task("fresh_session") is True
    assert runtime_resolver_requires_initial_task("delegation") is True


def test_requires_initial_task_invalid_response_fails_safe(monkeypatch, caplog):
    resolver = FakeResolver()
    resolver.requires_initial_task = lambda _scope: "yes"
    _install_resolver(monkeypatch, resolver)
    caplog.set_level("WARNING")

    assert runtime_resolver_requires_initial_task("fresh_session") is False
    assert "yes" not in caplog.text


@pytest.mark.parametrize(
    ("mutate", "effective"),
    [
        (lambda request: replace(request, context=replace(request.context, session_id="session-b")), None),
        (lambda request: replace(request, context=replace(request.context, task="different task")), None),
        (lambda request: replace(request, context=replace(request.context, operation_id="operation-b")), None),
        (lambda request: replace(request, baseline=replace(request.baseline, model="base-b")), None),
        (lambda request: replace(request, baseline=replace(request.baseline, api_key="other-secret")), None),
        (lambda request: replace(request, baseline=replace(request.baseline, credential_pool=object())), None),
        (lambda request: request, _projected_spec(model="substituted-model")),
    ],
)
def test_prepared_runtime_cannot_be_reused_across_request_or_runtime(
    monkeypatch, mutate, effective
):
    plan = _project_plan()
    _install_resolver(monkeypatch, FakeResolver(plan=plan))
    original = _request(context=_context(operation_id="operation-a"))
    prepared = prepare_agent_runtime(original)

    with pytest.raises(InvalidPreparedAgentRuntime, match="prepared runtime"):
        finalize_prepared_agent_runtime(
            prepared,
            mutate(original),
            effective or plan.runtime,
        )


def test_prepared_runtime_is_profile_bound(monkeypatch, tmp_path):
    plan = _project_plan()
    _install_resolver(monkeypatch, FakeResolver(plan=plan))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile-a"))
    request = _request()
    prepared = prepare_agent_runtime(request)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile-b"))

    with pytest.raises(InvalidPreparedAgentRuntime):
        finalize_prepared_agent_runtime(prepared, request, plan.runtime)


def test_prepared_fingerprint_distinguishes_mapping_key_types(monkeypatch):
    manager = PluginManager()
    manager._discovered = True
    monkeypatch.setattr("hermes_cli.plugins._plugin_manager", manager)
    original = _request(baseline=_spec(credential_pool={1: "pool"}))
    prepared = prepare_agent_runtime(original)
    substituted = replace(
        original,
        baseline=replace(original.baseline, credential_pool={"1": "pool"}),
    )

    with pytest.raises(InvalidPreparedAgentRuntime):
        finalize_prepared_agent_runtime(prepared, substituted, original.baseline)


def test_prepared_fingerprint_binds_equal_mapping_pools_by_opaque_identity(monkeypatch):
    manager = PluginManager()
    manager._discovered = True
    monkeypatch.setattr("hermes_cli.plugins._plugin_manager", manager)
    first_pool = {"provider": "custom:pool", "slot": 1}
    equal_but_distinct_pool = {"provider": "custom:pool", "slot": 1}
    original = _request(baseline=_spec(credential_pool=first_pool))
    prepared = prepare_agent_runtime(original)
    substituted = replace(
        original,
        baseline=replace(
            original.baseline, credential_pool=equal_but_distinct_pool
        ),
    )

    with pytest.raises(InvalidPreparedAgentRuntime):
        finalize_prepared_agent_runtime(prepared, substituted, original.baseline)


def test_prepared_fingerprint_authenticates_context_metadata(monkeypatch):
    manager = PluginManager()
    manager._discovered = True
    monkeypatch.setattr("hermes_cli.plugins._plugin_manager", manager)
    original = _request()
    prepared = prepare_agent_runtime(original)
    substituted = replace(
        original,
        context=replace(
            original.context,
            metadata={"domains": ["legal"], "complexity": 0.1},
        ),
    )

    with pytest.raises(InvalidPreparedAgentRuntime):
        finalize_prepared_agent_runtime(prepared, substituted, original.baseline)


@pytest.mark.parametrize(
    "tamper",
    [
        lambda value: replace(value, seal=b"forged"),
        lambda value: replace(
            value,
            prepared_for=replace(value.prepared_for, session_id="forged-session"),
        ),
        lambda value: replace(
            value,
            plan=replace(value.plan, decision_id="forged-decision"),
        ),
        lambda value: replace(
            value,
            requested_baseline=replace(value.requested_baseline, api_key="forged-key"),
        ),
        lambda value: replace(
            value,
            effective_runtime=replace(value.effective_runtime, model="forged-model"),
        ),
        lambda value: replace(value, effective_runtime_fingerprint="f" * 64),
    ],
)
def test_finalized_preparation_rejects_internal_tampering(monkeypatch, tamper):
    plan = _project_plan()
    _install_resolver(monkeypatch, FakeResolver(plan=plan))
    request = _request()
    finalized = finalize_prepared_agent_runtime(
        prepare_agent_runtime(request), request, plan.runtime
    )

    with pytest.raises(InvalidPreparedAgentRuntime):
        validate_prepared_agent_runtime(
            tamper(finalized), context=request.context, effective_runtime=plan.runtime
        )


def test_project_finalization_requires_resolved_exact_selected_runtime(monkeypatch):
    selected = _projected_spec()
    _install_resolver(monkeypatch, FakeResolver(plan=_project_plan(selected)))
    request = _request()
    prepared = prepare_agent_runtime(request)

    with pytest.raises(InvalidPreparedAgentRuntime):
        finalize_prepared_agent_runtime(
            prepared,
            request,
            replace(selected, resolution_state="requested"),
        )


@pytest.mark.parametrize("action", ["inherit", "shadow"])
def test_inherit_and_shadow_can_finalize_against_normally_resolved_baseline(
    monkeypatch, action
):
    requested = _spec(api_key=None, base_url="", resolution_state="requested")
    resolved = _spec(api_key="resolved-secret", base_url="https://resolved.invalid/v1")
    plan = AgentRuntimePlan(
        action=action,
        runtime=requested,
        reason_code="shadow_recorded" if action == "shadow" else "baseline_inherit",
    )
    _install_resolver(monkeypatch, FakeResolver(plan=plan))
    request = _request(baseline=requested)

    finalized = finalize_prepared_agent_runtime(
        prepare_agent_runtime(request), request, resolved
    )

    assert finalized.effective_runtime_fingerprint


@pytest.mark.parametrize("action", ["inherit", "shadow"])
def test_direct_context_resolves_baseline_then_finalizes_exact_runtime_before_client(
    monkeypatch, action
):
    order = []
    pool = object()
    resolver = FakeResolver(
        plan=lambda request: (
            order.append("policy"),
            AgentRuntimePlan(
                action=action,
                runtime=request.baseline,
                reason_code=(
                    "shadow_recorded" if action == "shadow" else "baseline_inherit"
                ),
            ),
        )[1]
    )
    _install_resolver(monkeypatch, resolver)

    def resolve_runtime_provider(**kwargs):
        order.append("hermes-runtime")
        assert kwargs["requested"] == "custom:primary"
        return {
            "provider": "custom:primary",
            "api_mode": "chat_completions",
            "base_url": "https://resolved.invalid/v1",
            "api_key": "resolved-secret",
            "credential_pool": pool,
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        resolve_runtime_provider,
    )
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *_a, **_k: pytest.fail(
            "resolved direct handoff must not enter provider auto-selection"
        ),
    )

    class TrackingAgent(AIAgent):
        def __setattr__(self, name, value):
            if name == "model" and "model" not in self.__dict__:
                order.append("assign")
            super().__setattr__(name, value)

        def _create_openai_client(self, client_kwargs, **_kwargs):
            order.append("client")
            binding = self._runtime_routing_binding
            assert binding.runtime.api_key == "resolved-secret"
            assert binding.runtime.base_url == "https://resolved.invalid/v1"
            assert binding.runtime.credential_pool is pool
            assert client_kwargs["api_key"] == "resolved-secret"
            return SimpleNamespace()

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
    ):
        agent = TrackingAgent(
            model="primary-model",
            provider="custom:primary",
            runtime_routing_context=_context(),
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    assert order[0:3] == ["policy", "hermes-runtime", "assign"]
    assert order.index("assign") < order.index("client")
    assert agent.base_url == "https://resolved.invalid/v1"
    assert agent.api_key == "resolved-secret"
    assert agent._credential_pool is pool


@pytest.mark.parametrize("action", ["inherit", "shadow"])
def test_direct_context_preserves_explicit_api_mode_through_transport_and_client(
    monkeypatch, action
):
    resolver = FakeResolver(
        plan=lambda request: AgentRuntimePlan(
            action=action,
            runtime=request.baseline,
            reason_code=(
                "shadow_recorded" if action == "shadow" else "baseline_inherit"
            ),
        )
    )
    _install_resolver(monkeypatch, resolver)
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "provider": "custom:primary",
            "api_mode": "chat_completions",
            "base_url": "https://resolved.invalid/v1",
            "api_key": "resolved-secret",
        },
    )
    transport_modes = []
    client_modes = []

    class TrackingAgent(AIAgent):
        def _get_transport(self, api_mode=None):
            transport_modes.append(api_mode or self.api_mode)
            return SimpleNamespace()

        def _create_openai_client(self, *_args, **_kwargs):
            client_modes.append(self.api_mode)
            return SimpleNamespace()

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
    ):
        agent = TrackingAgent(
            model="primary-model",
            provider="custom:primary",
            base_url="https://requested.invalid/v1",
            api_key="requested-secret",
            api_mode="codex_responses",
            runtime_routing_context=_context(),
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    binding = agent._runtime_routing_binding
    assert binding.runtime.api_mode == agent.api_mode == "codex_responses"
    assert transport_modes and set(transport_modes) == {"codex_responses"}
    assert client_modes == ["codex_responses"]


def test_force_reload_waits_for_in_flight_resolver_call_before_close(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    force_done = threading.Event()
    errors = []
    prepared_values = []

    class BlockingResolver(FakeResolver):
        def resolve(self, request):
            entered.set()
            if not release.wait(2):
                raise AssertionError("test did not release resolver")
            return AgentRuntimePlan(
                action="inherit",
                runtime=request.baseline,
                reason_code="baseline_inherit",
            )

    resolver = BlockingResolver()
    manager = _install_resolver(monkeypatch, resolver)
    replacement = FakeResolver()

    def rediscover():
        PluginContext(
            PluginManifest(name="replacement", key="replacement"), manager
        ).register_agent_runtime_resolver(replacement)

    def resolve_runtime():
        try:
            prepared_values.append(prepare_agent_runtime(_request()))
        except BaseException as exc:
            errors.append(exc)

    def force_reload():
        try:
            manager.discover_and_load(force=True)
        except BaseException as exc:
            errors.append(exc)
        finally:
            force_done.set()

    monkeypatch.setattr(manager, "_discover_and_load_inner", rediscover)
    resolve_thread = threading.Thread(target=resolve_runtime, daemon=True)
    force_thread = threading.Thread(target=force_reload, daemon=True)
    resolve_thread.start()
    assert entered.wait(1)
    force_thread.start()
    force_finished_during_resolve = force_done.wait(0.1)
    closed_during_resolve = resolver.closed
    release.set()
    resolve_thread.join(2)
    force_thread.join(2)

    assert not force_finished_during_resolve
    assert closed_during_resolve == 0
    assert not errors
    assert len(prepared_values) == 1
    assert resolver.closed == 1
    assert manager.agent_runtime_resolver is replacement


def test_resolver_worker_can_reenter_manager_while_parent_call_is_leased(
    monkeypatch,
):
    import hermes_cli.plugins as plugins_mod

    worker_results = []
    worker_errors = []
    worker_finished_during_resolve = []

    class WorkerResolver(FakeResolver):
        def resolve(self, request):
            worker_done = threading.Event()

            def read_manager():
                try:
                    worker_results.append(plugins_mod.get_agent_runtime_resolver())
                except BaseException as exc:
                    worker_errors.append(exc)
                finally:
                    worker_done.set()

            worker = threading.Thread(target=read_manager, daemon=True)
            worker.start()
            worker_finished_during_resolve.append(worker_done.wait(0.2))
            worker.join(1)
            return AgentRuntimePlan(
                action="inherit",
                runtime=request.baseline,
                reason_code="baseline_inherit",
            )

    resolver = WorkerResolver()
    _install_resolver(monkeypatch, resolver)

    prepared = prepare_agent_runtime(_request())

    assert prepared.plan.reason_code == "baseline_inherit"
    assert worker_finished_during_resolve == [True]
    assert worker_errors == []
    assert worker_results == [resolver]


def test_same_thread_force_reload_is_rejected_without_closing_active_resolver(
    monkeypatch,
):
    reload_errors = []
    closed_during_resolve = []

    class ReentrantForceResolver(FakeResolver):
        def __init__(self):
            super().__init__()
            self.manager = None

        def resolve(self, request):
            try:
                self.manager.discover_and_load(force=True)
            except RuntimeError as exc:
                reload_errors.append(str(exc))
            closed_during_resolve.append(self.closed)
            return AgentRuntimePlan(
                action="inherit",
                runtime=request.baseline,
                reason_code="baseline_inherit",
            )

    resolver = ReentrantForceResolver()
    manager = _install_resolver(monkeypatch, resolver)
    resolver.manager = manager
    monkeypatch.setattr(
        manager,
        "_discover_and_load_inner",
        lambda: pytest.fail("reentrant force must not start discovery"),
    )

    prepared = prepare_agent_runtime(_request())

    assert prepared.plan.reason_code == "baseline_inherit"
    assert reload_errors == ["Cannot reload plugins during an active resolver call"]
    assert closed_during_resolve == [0]
    assert resolver.closed == 0
    assert manager.agent_runtime_resolver is resolver


@pytest.mark.parametrize("action", ["inherit", "shadow"])
def test_bound_baseline_is_canonicalized_before_seal_and_assignment(
    monkeypatch, action
):
    calls = []
    resolver = FakeResolver(
        plan=lambda request: AgentRuntimePlan(
            action=action,
            runtime=request.baseline,
            reason_code=(
                "shadow_recorded" if action == "shadow" else "baseline_inherit"
            ),
        )
    )
    _install_resolver(monkeypatch, resolver)

    def resolve_runtime_provider(**kwargs):
        calls.append(dict(kwargs))
        assert kwargs == {
            "requested": "xiaomi",
            "explicit_api_key": "bound-secret",
            "explicit_base_url": "https://bound.invalid/v1",
            "target_model": "MiMo-V2.5-Pro",
        }
        return {
            "provider": "xiaomi",
            "api_mode": "chat_completions",
            "base_url": "https://bound.invalid/v1",
            "api_key": "bound-secret",
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        resolve_runtime_provider,
    )
    monkeypatch.setattr(
        AIAgent,
        "_create_openai_client",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
    ):
        agent = AIAgent(
            model="MiMo-V2.5-Pro",
            provider="xiaomi",
            base_url="https://bound.invalid/v1",
            api_key="bound-secret",
            runtime_routing_context=_context(),
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    binding = agent._runtime_routing_binding
    assert len(calls) == 1
    assert binding.runtime.model == agent.model == "mimo-v2.5-pro"
    assert binding.runtime.provider == agent.provider == "xiaomi"
    assert binding.runtime.api_mode == agent.api_mode == "chat_completions"
    assert binding.runtime.base_url == agent.base_url
    assert binding.runtime.api_key == agent.api_key


def test_bound_query_endpoint_keeps_exact_live_identity_and_clean_client_url(
    monkeypatch,
):
    exact_url = "https://azure.invalid/openai/v1?api-version=2026-01-01"
    _install_resolver(monkeypatch, FakeResolver())

    def resolve_runtime_provider(**kwargs):
        assert kwargs["explicit_base_url"] == exact_url
        return {
            "provider": "custom:azure",
            "api_mode": "chat_completions",
            "base_url": exact_url,
            "api_key": "azure-secret",
        }

    client_kwargs_seen = []
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        resolve_runtime_provider,
    )
    monkeypatch.setattr(
        AIAgent,
        "_create_openai_client",
        lambda _agent, kwargs, **_options: (
            client_kwargs_seen.append(dict(kwargs)) or SimpleNamespace()
        ),
    )

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
    ):
        agent = AIAgent(
            model="azure-model",
            provider="custom:azure",
            base_url=exact_url,
            api_key="azure-secret",
            runtime_routing_context=_context(),
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    binding = agent._runtime_routing_binding
    assert binding.runtime.base_url == agent.base_url == exact_url
    assert client_kwargs_seen == [
        {
            "api_key": "azure-secret",
            "base_url": "https://azure.invalid/openai/v1",
            "default_query": {"api-version": "2026-01-01"},
        }
    ]
    assert agent._client_kwargs == client_kwargs_seen[0]


def test_owned_explicit_local_endpoint_without_key_resolves_as_custom_primary(
    monkeypatch,
):
    baseline_url = "http://127.0.0.1:11434/v1"
    _install_resolver(
        monkeypatch,
        FakeResolver(
            plan=lambda request: AgentRuntimePlan(
                action="inherit",
                runtime=request.baseline,
                owns_fallbacks=True,
                reason_code="no_eligible_runtime",
            )
        ),
    )

    def resolve_runtime_provider(**kwargs):
        assert kwargs["requested"] == "custom"
        assert kwargs["explicit_base_url"] == baseline_url
        assert kwargs["explicit_api_key"] is None
        return {
            "provider": "custom",
            "api_mode": "chat_completions",
            "base_url": baseline_url,
            "api_key": "dummy-key",
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        resolve_runtime_provider,
    )
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *_args, **_kwargs: pytest.fail(
            "explicit local primary must not enter global provider selection"
        ),
    )
    monkeypatch.setattr(
        AIAgent,
        "_create_openai_client",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
    ):
        agent = AIAgent(
            model="local-model",
            provider="auto",
            base_url=baseline_url,
            runtime_routing_context=_context(),
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    assert agent.provider == "custom"
    assert agent.base_url == baseline_url
    assert agent.api_key == "dummy-key"
    assert agent._runtime_routing_binding.owns_fallbacks is True
    assert agent._fallback_chain == []


def test_host_fallback_selected_during_resolution_advances_live_chain_state(
    monkeypatch,
):
    fallbacks = [
        {"provider": "custom:malformed"},
        {"provider": 7, "model": "numeric-provider"},
        {"provider": " auto ", "model": "global-auto"},
        {"provider": " CUSTOM:FIRST ", "model": " first-model "},
        {"provider": "custom:second", "model": "second-model"},
    ]
    canonical_fallbacks = [
        {"provider": "custom:first", "model": "first-model"},
        {"provider": "custom:second", "model": "second-model"},
    ]
    _install_resolver(monkeypatch, FakeResolver())
    calls = []

    def resolve_runtime_provider(**kwargs):
        calls.append(kwargs["requested"])
        if kwargs["requested"] == "custom:primary":
            raise RuntimeError("primary unavailable")
        if kwargs["requested"] == "custom:first":
            return {
                "provider": "custom:first",
                "api_mode": "chat_completions",
                "base_url": "https://first.invalid/v1",
                "api_key": "first-secret",
            }
        raise AssertionError("later fallbacks must not resolve during construction")

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        resolve_runtime_provider,
    )
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *_args, **_kwargs: pytest.fail(
            "sealed fallback handoff must construct without auxiliary routing"
        ),
    )
    monkeypatch.setattr(
        AIAgent,
        "_create_openai_client",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
    ):
        agent = AIAgent(
            model="primary-model",
            provider="custom:primary",
            fallback_model=fallbacks,
            runtime_routing_context=_context(),
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    assert calls == ["custom:primary", "custom:first"]
    assert agent.provider == "custom:first"
    assert agent.model == "first-model"
    assert agent._fallback_chain == canonical_fallbacks
    assert tuple(agent._runtime_routing_binding.runtime.fallback_model) == tuple(
        canonical_fallbacks
    )
    assert agent._fallback_activated is True
    assert agent._fallback_index == 1


def test_owned_inherit_with_unbound_auto_primary_never_global_selects(monkeypatch):
    _install_resolver(
        monkeypatch,
        FakeResolver(
            plan=lambda request: AgentRuntimePlan(
                action="inherit",
                runtime=request.baseline,
                owns_fallbacks=True,
                reason_code="no_eligible_runtime",
            )
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: pytest.fail("owned auto runtime must not global-select"),
    )
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *_args, **_kwargs: pytest.fail(
            "owned auto runtime must not reach client global-selection"
        ),
    )

    with pytest.raises(RuntimeError, match="explicit primary runtime"):
        AIAgent(
            model="primary-model",
            provider="auto",
            fallback_model={"provider": "host", "model": "host-fallback"},
            runtime_routing_context=_context(),
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )


def test_finalized_inherit_host_wrapper_skips_policy_and_ordinary_resolution(
    monkeypatch,
):
    requested = _spec(
        model="primary-model",
        provider="custom:primary",
        base_url="",
        api_key=None,
        resolution_state="requested",
        resolution_reason_code="unresolved",
        fallback_model=(),
    )
    resolved = replace(
        requested,
        base_url="https://resolved.invalid/v1",
        api_key="resolved-secret",
        resolution_state="resolved",
        resolution_reason_code="",
    )
    resolver = FakeResolver(
        plan=AgentRuntimePlan(
            action="inherit",
            runtime=requested,
            reason_code="baseline_inherit",
        )
    )
    _install_resolver(monkeypatch, resolver)
    request = _request(context=_context(), baseline=requested)
    finalized = finalize_prepared_agent_runtime(
        prepare_agent_runtime(request), request, resolved
    )
    assert len(resolver.requests) == 1
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: pytest.fail("finalized host wrapper must not re-resolve"),
    )

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            model=resolved.model,
            provider=resolved.provider,
            base_url=resolved.base_url,
            api_key=resolved.api_key,
            api_mode=resolved.api_mode,
            runtime_routing_context=request.context,
            prepared_agent_runtime=finalized,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    assert len(resolver.requests) == 1
    assert agent._runtime_routing_binding.runtime.api_key == "resolved-secret"


def test_defer_is_typed_outcome_before_finalization_or_client(monkeypatch):
    plan = AgentRuntimePlan(
        action="defer",
        runtime=_spec(),
        reason_code="operation_pending",
        retry_after_seconds=0.25,
    )
    _install_resolver(monkeypatch, FakeResolver(plan=plan))

    with pytest.raises(RuntimeRoutingDeferred) as exc:
        prepare_agent_runtime(_request())

    assert exc.value.retry_after_seconds == 0.25
    assert "operation_pending" in str(exc.value)


def test_unfinalized_prepared_runtime_is_rejected_before_client(monkeypatch):
    manager = PluginManager()
    manager._discovered = True
    monkeypatch.setattr("hermes_cli.plugins._plugin_manager", manager)
    request = _request()
    prepared = prepare_agent_runtime(request)
    client_calls = []
    monkeypatch.setattr(
        AIAgent,
        "_create_openai_client",
        lambda *_a, **_k: client_calls.append(True),
    )
    with pytest.raises(InvalidPreparedAgentRuntime):
        AIAgent(
            model=request.baseline.model,
            provider=request.baseline.provider,
            base_url=request.baseline.base_url,
            api_key=request.baseline.api_key,
            runtime_routing_context=request.context,
            prepared_agent_runtime=prepared,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    assert client_calls == []


def test_prepared_runtime_without_context_is_rejected_before_client(monkeypatch):
    plan = _project_plan()
    _install_resolver(monkeypatch, FakeResolver(plan=plan))
    request = _request()
    prepared = finalize_prepared_agent_runtime(
        prepare_agent_runtime(request), request, plan.runtime
    )
    client_calls = []
    monkeypatch.setattr(
        AIAgent,
        "_create_openai_client",
        lambda *_a, **_k: client_calls.append(True),
    )

    with pytest.raises(InvalidPreparedAgentRuntime):
        AIAgent(
            model=plan.runtime.model,
            provider=plan.runtime.provider,
            base_url=plan.runtime.base_url,
            api_key=plan.runtime.api_key,
            prepared_agent_runtime=prepared,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    assert client_calls == []


def test_runtime_resolver_runs_before_assignment_or_client_and_projects_all_locals(
    monkeypatch
):
    order = []
    selected = _projected_spec()

    def plan_for(request):
        order.append("resolve")
        assert request.baseline.model == "baseline-model"
        assert request.baseline.api_key == "baseline-secret"
        return _project_plan(selected)

    _install_resolver(monkeypatch, FakeResolver(plan=plan_for))
    client_kwargs_seen = []

    def build_client(_agent, client_kwargs, **_kwargs):
        order.append("client")
        client_kwargs_seen.append(dict(client_kwargs))
        return SimpleNamespace()

    monkeypatch.setattr(AIAgent, "_create_openai_client", build_client)

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
    ):
        agent = AIAgent(
            model="baseline-model",
            provider="custom:baseline",
            base_url="https://baseline.invalid/v1",
            api_key="baseline-secret",
            acp_command="baseline-acp",
            acp_args=["--baseline-token"],
            reasoning_config={"effort": "low"},
            fallback_model={"provider": "host", "model": "host-fallback"},
            credential_pool=object(),
            runtime_routing_context=_context(),
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    assert order == ["resolve", "client"]
    assert agent.model == "selected-model"
    assert agent.provider == "custom:selected"
    assert agent.base_url == "https://selected.invalid/v1"
    assert agent.api_key == "selected-secret"
    assert agent.acp_command == "selected-acp"
    assert agent.acp_args == ["--token", "ACP_TOKEN_SENTINEL"]
    assert agent._credential_pool is selected.credential_pool
    assert dict(agent.reasoning_config) == {"effort": "high"}
    assert agent._fallback_chain[0]["model"] == "plugin-fallback"
    assert tuple(agent._fallback_chain) == tuple(
        dict(item) for item in agent._runtime_routing_binding.runtime.fallback_model
    )
    assert agent._runtime_fallback_authority == "plugin"
    assert client_kwargs_seen[0]["api_key"] == "selected-secret"
    assert client_kwargs_seen[0]["base_url"] == "https://selected.invalid/v1"
    assert public_runtime_binding(agent)["bound_route_identity"] == "route-a"


@pytest.mark.parametrize(
    "selected",
    [
        _projected_spec(
            base_url="",
            api_key=None,
            credential_pool=object(),
            acp_command=None,
            acp_args=(),
            fallback_model=(),
        ),
        _projected_spec(
            provider="copilot-acp",
            base_url="",
            api_key=None,
            credential_pool=None,
            acp_command="copilot",
            acp_args=("--stdio",),
            fallback_model=(),
        ),
        _projected_spec(
            provider="moa",
            base_url="",
            api_key=None,
            credential_pool=None,
            acp_command=None,
            acp_args=(),
            fallback_model=(),
        ),
        _projected_spec(
            provider="bedrock",
            base_url="",
            api_key=None,
            api_mode="bedrock_converse",
            credential_pool=None,
            acp_command=None,
            acp_args=(),
            fallback_model=(),
        ),
        _projected_spec(api_mode="", fallback_model=()),
        _projected_spec(api_mode="bedrock_converse", fallback_model=()),
        _projected_spec(api_mode="codex_app_server", fallback_model=()),
        _projected_spec(
            provider="copilot-acp",
            api_mode="bedrock_converse",
            acp_command="copilot",
            acp_args=("--stdio",),
            fallback_model=(),
        ),
    ],
    ids=[
        "pool-only",
        "acp-only",
        "moa-provider-only",
        "bedrock-provider-only",
        "blank-api-mode",
        "custom-bedrock-mode",
        "custom-app-server-mode",
        "acp-bedrock-mode",
    ],
)
def test_noncanonical_project_cannot_reach_any_runtime_client(
    monkeypatch, selected
):
    baseline = _spec()
    _install_resolver(monkeypatch, FakeResolver(plan=_project_plan(selected)))

    def resolve_runtime_provider(**kwargs):
        assert kwargs["requested"] == baseline.provider
        return {
            "provider": baseline.provider,
            "api_mode": baseline.api_mode,
            "base_url": baseline.base_url,
            "api_key": baseline.api_key,
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        resolve_runtime_provider,
    )
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *_args, **_kwargs: pytest.fail(
            "noncanonical project must not enter auxiliary provider routing"
        ),
    )
    monkeypatch.setattr(
        "agent.moa_loop.MoAClient",
        lambda *_args, **_kwargs: pytest.fail(
            "noncanonical MoA project must not construct a MoA client"
        ),
    )
    monkeypatch.setattr(
        AIAgent,
        "_create_openai_client",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
    ):
        agent = AIAgent(
            model=baseline.model,
            provider=baseline.provider,
            base_url=baseline.base_url,
            api_key=baseline.api_key,
            api_mode=baseline.api_mode,
            runtime_routing_context=_context(),
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    binding = agent._runtime_routing_binding
    assert binding.action == "inherit"
    assert binding.reason_code == "resolver_contract_invalid"
    assert binding.runtime.model == agent.model == baseline.model
    assert binding.runtime.provider == agent.provider == baseline.provider
    assert binding.runtime.base_url == agent.base_url == baseline.base_url
    assert binding.runtime.api_key == agent.api_key == baseline.api_key
    assert binding.runtime.api_mode == agent.api_mode == baseline.api_mode


def test_incompatible_sealed_pool_is_rejected_before_client(monkeypatch):
    class IncompatiblePool:
        provider = "custom:other"

    selected = _projected_spec(credential_pool=IncompatiblePool())
    _install_resolver(monkeypatch, FakeResolver(plan=_project_plan(selected)))
    client_calls = []
    monkeypatch.setattr(
        AIAgent,
        "_create_openai_client",
        lambda *_args, **_kwargs: client_calls.append(True),
    )

    with pytest.raises(InvalidPreparedAgentRuntime):
        AIAgent(
            model="baseline-model",
            provider="custom:baseline",
            base_url="https://baseline.invalid/v1",
            api_key="baseline-secret",
            runtime_routing_context=_context(),
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    assert client_calls == []


def test_canonical_bedrock_project_binding_matches_live_agent_runtime(monkeypatch):
    selected = _projected_spec(
        model="amazon.nova-pro-v1:0",
        provider="bedrock",
        base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
        api_key="aws-sdk",
        api_mode="bedrock_converse",
        acp_command=None,
        acp_args=(),
        credential_pool=None,
        fallback_model=(),
    )
    _install_resolver(monkeypatch, FakeResolver(plan=_project_plan(selected)))
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *_args, **_kwargs: pytest.fail(
            "canonical Bedrock project must not enter auxiliary routing"
        ),
    )
    monkeypatch.setattr(
        AIAgent,
        "_create_openai_client",
        lambda *_args, **_kwargs: pytest.fail(
            "Bedrock Converse must not construct an OpenAI client"
        ),
    )

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
    ):
        agent = AIAgent(
            model="baseline-model",
            provider="custom:baseline",
            base_url="https://baseline.invalid/v1",
            api_key="baseline-secret",
            runtime_routing_context=_context(),
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    binding = agent._runtime_routing_binding
    assert binding.runtime.model == agent.model == selected.model
    assert binding.runtime.provider == agent.provider == selected.provider
    assert binding.runtime.base_url == agent.base_url == selected.base_url
    assert binding.runtime.api_key == agent.api_key == selected.api_key
    assert binding.runtime.api_mode == agent.api_mode == selected.api_mode


def test_canonical_anthropic_bedrock_binding_matches_live_agent_runtime(monkeypatch):
    selected = _projected_spec(
        model="anthropic.claude-sonnet-4-20250514-v1:0",
        provider="bedrock",
        base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
        api_key="aws-sdk",
        api_mode="anthropic_messages",
        acp_command=None,
        acp_args=(),
        credential_pool=None,
        fallback_model=(),
    )
    _install_resolver(monkeypatch, FakeResolver(plan=_project_plan(selected)))
    monkeypatch.setattr(
        "agent.anthropic_adapter.build_anthropic_bedrock_client",
        lambda region: SimpleNamespace(region=region),
    )
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *_args, **_kwargs: pytest.fail(
            "canonical Anthropic Bedrock project must not enter auxiliary routing"
        ),
    )

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
    ):
        agent = AIAgent(
            model="baseline-model",
            provider="custom:baseline",
            base_url="https://baseline.invalid/v1",
            api_key="baseline-secret",
            runtime_routing_context=_context(),
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    binding = agent._runtime_routing_binding
    assert binding.runtime.model == agent.model == selected.model
    assert binding.runtime.provider == agent.provider == selected.provider
    assert binding.runtime.base_url == agent.base_url == selected.base_url
    assert binding.runtime.api_key == agent.api_key == selected.api_key
    assert binding.runtime.api_mode == agent.api_mode == selected.api_mode
    assert agent._anthropic_base_url == selected.base_url


def test_minimax_oauth_adapter_is_canonicalized_once_before_sealing(monkeypatch):
    class TokenProvider:
        def __call__(self):
            return "fresh-token"

    token_provider = TokenProvider()
    adapter_builds = []
    _install_resolver(monkeypatch, FakeResolver())
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "provider": "minimax-oauth",
            "api_mode": "anthropic_messages",
            "base_url": "https://api.minimax.invalid/anthropic",
            "api_key": "short-lived-token",
        },
    )

    def build_token_provider():
        adapter_builds.append(True)
        return token_provider

    client_keys = []
    monkeypatch.setattr(
        "hermes_cli.auth.build_minimax_oauth_token_provider",
        build_token_provider,
    )
    monkeypatch.setattr(
        "agent.anthropic_adapter.build_anthropic_client",
        lambda key, _url, **_kwargs: (
            client_keys.append(key) or SimpleNamespace()
        ),
    )

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
    ):
        agent = AIAgent(
            model="minimax-model",
            provider="minimax-oauth",
            base_url="https://api.minimax.invalid/anthropic",
            api_key="short-lived-token",
            runtime_routing_context=_context(),
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    binding = agent._runtime_routing_binding
    assert adapter_builds == [True]
    assert binding.runtime.api_key is token_provider
    assert agent.api_key is token_provider
    assert client_keys == [token_provider]


def test_runtime_projection_returns_new_constructor_mapping_without_mutating_input():
    selected = _projected_spec()
    binding = RuntimeRoutingBinding(
        scope="fresh_session",
        session_id="session-a",
        task_id="task-a",
        operation_id=None,
        action="project",
        runtime=selected,
        owns_fallbacks=True,
        reason_code="active_projected",
    )
    original = {
        "model": "baseline",
        "provider": "custom:baseline",
        "base_url": "https://baseline.invalid/v1",
        "api_key": "baseline-secret",
        "fallback_model": {"provider": "host", "model": "host-fallback"},
        "command": "baseline-command",
        "args": ["--baseline"],
    }

    projected = apply_runtime_plan_to_constructor_arguments(
        original, binding=binding, effective_runtime=selected
    )

    assert projected is not original
    assert original["model"] == "baseline"
    assert original["command"] == "baseline-command"
    assert projected["model"] == "selected-model"
    assert projected["api_key"] == "selected-secret"
    assert projected["command"] is None
    assert projected["args"] is None
    assert projected["fallback_model"][0]["model"] == "plugin-fallback"


def test_finalized_host_wrapper_constructs_successfully_without_second_resolution(
    monkeypatch,
):
    selected = _projected_spec()
    resolver = FakeResolver(plan=_project_plan(selected))
    _install_resolver(monkeypatch, resolver)
    request = _request()
    finalized = finalize_prepared_agent_runtime(
        prepare_agent_runtime(request), request, selected
    )

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            model=selected.model,
            provider=selected.provider,
            base_url=selected.base_url,
            api_key=selected.api_key,
            api_mode=selected.api_mode,
            acp_command=selected.acp_command,
            acp_args=list(selected.acp_args),
            credential_pool=selected.credential_pool,
            reasoning_config=dict(selected.reasoning_config or {}),
            fallback_model=[dict(item) for item in selected.fallback_model],
            runtime_routing_context=request.context,
            prepared_agent_runtime=finalized,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    assert len(resolver.requests) == 1
    assert agent.model == selected.model
    assert isinstance(agent._runtime_routing_binding, RuntimeRoutingBinding)
    assert agent._session_init_model_config["model"] == "selected-model"
    assert agent._session_init_model_config["provider"] == "custom:selected"
    assert agent._session_init_model_config["base_url"] == (
        "https://selected.invalid/v1"
    )
    assert agent._session_init_model_config["api_mode"] == "chat_completions"
    assert agent._session_init_model_config["reasoning_config"] == {
        "effort": "high"
    }
    persisted = json.dumps(agent._session_init_model_config)
    assert "selected-secret" not in persisted
    assert "ACP_TOKEN_SENTINEL" not in persisted
    assert "FALLBACK_KEY_SENTINEL" not in persisted


@pytest.mark.parametrize(
    "override",
    [
        {"model": "other-model"},
        {"provider": "custom:other"},
        {"base_url": "https://other.invalid/v1"},
        {"api_key": "other-key"},
        {"api_mode": "codex_responses"},
        {"acp_command": "other-acp"},
        {"acp_args": ["--other"]},
        {"credential_pool": object()},
        {"reasoning_config": {"effort": "low"}},
        {"fallback_model": []},
    ],
)
def test_host_wrapper_rejects_every_effective_constructor_field_mismatch(
    monkeypatch, override
):
    selected = _projected_spec()
    resolver = FakeResolver(plan=_project_plan(selected))
    _install_resolver(monkeypatch, resolver)
    request = _request()
    finalized = finalize_prepared_agent_runtime(
        prepare_agent_runtime(request), request, selected
    )
    kwargs = {
        "model": selected.model,
        "provider": selected.provider,
        "base_url": selected.base_url,
        "api_key": selected.api_key,
        "api_mode": selected.api_mode,
        "acp_command": selected.acp_command,
        "acp_args": list(selected.acp_args),
        "credential_pool": selected.credential_pool,
        "reasoning_config": dict(selected.reasoning_config or {}),
        "fallback_model": [dict(item) for item in selected.fallback_model],
    }
    kwargs.update(override)
    client_calls = []
    monkeypatch.setattr(
        AIAgent,
        "_create_openai_client",
        lambda *_a, **_k: client_calls.append(True),
    )

    with pytest.raises(InvalidPreparedAgentRuntime):
        AIAgent(
            **kwargs,
            runtime_routing_context=request.context,
            prepared_agent_runtime=finalized,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    assert client_calls == []


@pytest.mark.parametrize(
    "context",
    [
        replace(_context(), scope="delegation"),
        replace(_context(), session_id="other-session"),
        replace(_context(), task_id="other-task-id"),
        replace(_context(), task="other task"),
        replace(_context(), operation_id="other-operation"),
        replace(_context(), task_index=3),
        replace(_context(), is_resume=True),
        replace(
            _context(), manual_runtime_pin=True, manual_pin_source="model_command"
        ),
    ],
)
def test_host_wrapper_rejects_every_context_identity_mismatch(monkeypatch, context):
    selected = _projected_spec()
    _install_resolver(monkeypatch, FakeResolver(plan=_project_plan(selected)))
    request = _request()
    finalized = finalize_prepared_agent_runtime(
        prepare_agent_runtime(request), request, selected
    )
    client_calls = []
    monkeypatch.setattr(
        AIAgent,
        "_create_openai_client",
        lambda *_a, **_k: client_calls.append(True),
    )

    with pytest.raises(InvalidPreparedAgentRuntime):
        AIAgent(
            model=selected.model,
            provider=selected.provider,
            base_url=selected.base_url,
            api_key=selected.api_key,
            api_mode=selected.api_mode,
            acp_command=selected.acp_command,
            acp_args=list(selected.acp_args),
            credential_pool=selected.credential_pool,
            reasoning_config=dict(selected.reasoning_config or {}),
            fallback_model=[dict(item) for item in selected.fallback_model],
            runtime_routing_context=context,
            prepared_agent_runtime=finalized,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    assert client_calls == []


def test_resolver_precedes_runtime_assignment_transport_and_client(monkeypatch):
    order = []
    selected = _projected_spec(acp_command=None, acp_args=(), credential_pool=None)

    def route(_request):
        order.append("resolve")
        return _project_plan(selected)

    _install_resolver(monkeypatch, FakeResolver(plan=route))

    class TrackingAgent(AIAgent):
        def __setattr__(self, name, value):
            if name == "model" and "model" not in self.__dict__:
                order.append("assign")
            super().__setattr__(name, value)

        def _get_transport(self):
            order.append("transport")
            return SimpleNamespace()

        def _create_openai_client(self, *_args, **_kwargs):
            order.append("client")
            return SimpleNamespace()

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
    ):
        TrackingAgent(
            model="baseline-model",
            provider="custom:baseline",
            base_url="https://baseline.invalid/v1",
            api_key="baseline-secret",
            api_mode="chat_completions",
            runtime_routing_context=_context(),
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    assert order[0:2] == ["resolve", "assign"]
    assert order.index("resolve") < order.index("transport") < order.index("client")


def test_defer_from_aiagent_occurs_before_assignment_transport_or_client(monkeypatch):
    _install_resolver(
        monkeypatch,
        FakeResolver(
            plan=AgentRuntimePlan(
                action="defer",
                runtime=_spec(),
                reason_code="operation_pending",
                retry_after_seconds=0.5,
            )
        ),
    )
    touched = []

    class TrackingAgent(AIAgent):
        def __setattr__(self, name, value):
            if name in {"model", "provider", "client"}:
                touched.append(name)
            super().__setattr__(name, value)

        def _get_transport(self):
            touched.append("transport")
            return SimpleNamespace()

        def _create_openai_client(self, *_args, **_kwargs):
            touched.append("client_factory")
            return SimpleNamespace()

    with pytest.raises(RuntimeRoutingDeferred):
        TrackingAgent(
            model="baseline-model",
            provider="custom:baseline",
            base_url="https://baseline.invalid/v1",
            api_key="baseline-secret",
            api_mode="chat_completions",
            runtime_routing_context=_context(),
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    assert touched == []


def test_agent_without_routing_context_never_invokes_registered_resolver(monkeypatch):
    resolver = FakeResolver(plan=_project_plan())
    _install_resolver(monkeypatch, resolver)

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            model="baseline-model",
            provider="custom:baseline",
            base_url="https://baseline.invalid/v1",
            api_key="baseline-secret",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    assert resolver.requests == []
    assert agent.model == "baseline-model"
    assert agent._runtime_routing_binding is None
    assert agent._runtime_fallback_authority == "host"


def test_agent_without_context_never_even_queries_plugin_discovery(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.plugins.get_agent_runtime_resolver",
        lambda: pytest.fail("resolver getter must not run without a routing context"),
    )
    monkeypatch.setattr(
        "agent.runtime_routing.constructor_runtime_spec",
        lambda **_kwargs: pytest.fail(
            "ordinary construction must not enter runtime routing helpers"
        ),
    )
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        AIAgent(
            model="baseline-model",
            provider="custom:baseline",
            base_url="https://baseline.invalid/v1",
            api_key="baseline-secret",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )


def test_finalized_wrapper_tamper_is_rejected_before_client(monkeypatch):
    plan = _project_plan()
    _install_resolver(monkeypatch, FakeResolver(plan=plan))
    request = _request()
    finalized = finalize_prepared_agent_runtime(
        prepare_agent_runtime(request), request, plan.runtime
    )
    tampered = replace(finalized, effective_runtime_fingerprint="0" * 64)
    client_calls = []
    monkeypatch.setattr(
        AIAgent,
        "_create_openai_client",
        lambda *_a, **_k: client_calls.append(True),
    )

    with pytest.raises(InvalidPreparedAgentRuntime):
        AIAgent(
            model=plan.runtime.model,
            provider=plan.runtime.provider,
            base_url=plan.runtime.base_url,
            api_key=plan.runtime.api_key,
            acp_command=plan.runtime.acp_command,
            acp_args=list(plan.runtime.acp_args),
            reasoning_config=dict(plan.runtime.reasoning_config or {}),
            fallback_model=[dict(v) for v in plan.runtime.fallback_model],
            credential_pool=plan.runtime.credential_pool,
            runtime_routing_context=request.context,
            prepared_agent_runtime=tampered,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    assert client_calls == []


def test_public_binding_omits_all_opaque_execution_fields(monkeypatch):
    selected = _projected_spec()
    _install_resolver(monkeypatch, FakeResolver(plan=_project_plan(selected)))
    request = _request()
    prepared = finalize_prepared_agent_runtime(
        prepare_agent_runtime(request), request, selected
    )

    public = public_runtime_binding(prepared)
    rendered = repr(public)

    assert "base_url" not in public
    assert "api_key" not in public
    assert "credential_pool" not in public
    assert "acp_args" not in public
    assert "fallback_model" not in public
    assert "reasoning_config" not in public
    assert "acp_command" not in public
    for secret in (
        "selected-secret",
        "selected.invalid",
        "ACP_TOKEN_SENTINEL",
        "FALLBACK_KEY_SENTINEL",
    ):
        assert secret not in rendered
    assert selected.public_record() == {
        "model": "selected-model",
        "provider": "custom:selected",
        "api_mode": "chat_completions",
        "resolution_state": "resolved",
        "resolution_reason_code": "",
    }
    assert prepared.public_record() == public
    assert json.loads(json.dumps(public))["event"] == {
        "profile_id": "balanced",
        "candidate_count": 2,
    }


def test_public_binding_rejects_unfinalized_or_tampered_preparation(monkeypatch):
    selected = _projected_spec()
    _install_resolver(monkeypatch, FakeResolver(plan=_project_plan(selected)))
    request = _request()
    unfinalized = prepare_agent_runtime(request)

    with pytest.raises(InvalidPreparedAgentRuntime):
        public_runtime_binding(unfinalized)

    finalized = finalize_prepared_agent_runtime(unfinalized, request, selected)
    with pytest.raises(InvalidPreparedAgentRuntime):
        public_runtime_binding(replace(finalized, seal=b"wrong-seal"))


def test_public_binding_hostile_mapping_returns_safe_empty_without_echo(caplog):
    caplog.set_level("WARNING")
    binding = RuntimeRoutingBinding(
        scope="fresh_session",
        session_id="session-a",
        task_id="task-a",
        operation_id=None,
        action="inherit",
        runtime=_spec(),
        reason_code="baseline_inherit",
        event=HostileMapping(),
    )

    assert public_runtime_binding(binding) == {}
    assert "HOSTILE_MAPPING_SECRET" not in caplog.text


def test_public_binding_revalidates_direct_event_and_identifier_secrets():
    secret_event = RuntimeRoutingBinding(
        scope="fresh_session",
        session_id="session-a",
        task_id="task-a",
        operation_id=None,
        action="inherit",
        runtime=_spec(),
        reason_code="baseline_inherit",
        event={"api_key": "DIRECT_EVENT_SECRET"},
    )
    secret_identifier = replace(
        secret_event,
        session_id="ghp_0123456789ABCDEF",
        event={},
    )

    assert public_runtime_binding(secret_event) == {}
    assert public_runtime_binding(secret_identifier) == {}


@pytest.mark.parametrize(
    "tamper",
    [
        lambda binding: replace(binding, scope="ghp_0123456789ABCDEF"),
        lambda binding: replace(binding, action="arbitrary-action"),
        lambda binding: replace(binding, action="defer"),
        lambda binding: replace(binding, owns_fallbacks=1),
        lambda binding: replace(
            binding,
            action="project",
            reason_code="active_projected",
            runtime=replace(
                binding.runtime,
                provider="auto",
                resolution_state="requested",
                api_mode="",
            ),
        ),
        lambda binding: replace(binding, reason_code="active_projected"),
    ],
)
def test_public_binding_revalidates_direct_scope_action_and_ownership(tamper):
    binding = RuntimeRoutingBinding(
        scope="fresh_session",
        session_id="session-a",
        task_id="task-a",
        operation_id=None,
        action="inherit",
        runtime=_spec(),
        owns_fallbacks=False,
        reason_code="baseline_inherit",
        event={},
    )

    assert public_runtime_binding(tamper(binding)) == {}


def test_public_binding_allows_typed_model_identifier_with_slash():
    binding = RuntimeRoutingBinding(
        scope="fresh_session",
        session_id="session-a",
        task_id="task-a",
        operation_id=None,
        action="inherit",
        runtime=_spec(model="openai/gpt-5.4"),
        reason_code="baseline_inherit",
        event={"profile_id": "balanced"},
    )

    assert public_runtime_binding(binding)["model"] == "openai/gpt-5.4"


@pytest.mark.parametrize(
    "command",
    [
        "/opt/github-copilot/bin/copilot",
        r"C:\Program Files\GitHub Copilot\copilot.exe",
    ],
    ids=["posix-absolute", "windows-absolute"],
)
def test_public_binding_keeps_absolute_acp_executable_opaque(command):
    runtime = AgentRuntimeSpec(
        model="gpt-4.1",
        provider="copilot-acp",
        base_url="acp://copilot",
        api_key="copilot-acp-runtime",
        resolution_state="resolved",
        api_mode="chat_completions",
        acp_command=command,
        acp_args=("--stdio",),
    )
    binding = RuntimeRoutingBinding(
        scope="fresh_session",
        session_id="session-a",
        task_id="task-a",
        operation_id=None,
        action="inherit",
        runtime=runtime,
        reason_code="baseline_inherit",
        event={"profile_id": "balanced"},
    )

    public = public_runtime_binding(binding)
    rendered = json.dumps(public)

    assert public
    assert public["provider"] == "copilot-acp"
    assert "acp_command" not in public
    assert command not in rendered
    assert public_runtime_binding(replace(binding, event={"executable": command})) == {}


def test_fallback_runtime_uses_selected_provider_api_mode_not_primary_mode(
    monkeypatch,
):
    _install_resolver(monkeypatch, FakeResolver())

    def resolve(*, requested, **_kwargs):
        if requested == "fallback-provider":
            return {
                "provider": "fallback-provider",
                "api_mode": "chat_completions",
                "base_url": "https://fallback.invalid/v1",
                "api_key": "fallback-secret",
            }
        raise RuntimeError("primary unavailable")

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider", resolve
    )
    client_modes = []
    monkeypatch.setattr(
        AIAgent,
        "_create_openai_client",
        lambda agent, *_args, **_kwargs: (
            client_modes.append(agent.api_mode) or SimpleNamespace()
        ),
    )

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
    ):
        agent = AIAgent(
            model="primary-model",
            provider="missing-provider",
            base_url="https://primary.invalid/v1",
            api_key="primary-secret",
            api_mode="codex_responses",
            fallback_model={
                "provider": "fallback-provider",
                "model": "fallback-model",
            },
            runtime_routing_context=_context(),
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    binding = agent._runtime_routing_binding
    assert binding.runtime.provider == agent.provider == "fallback-provider"
    assert binding.runtime.api_mode == agent.api_mode == "chat_completions"
    assert client_modes == ["chat_completions"]


@pytest.mark.parametrize("owns", [False, True])
def test_inherit_fallback_ownership_controls_later_host_chain(monkeypatch, owns):
    baseline = _spec()
    _install_resolver(
        monkeypatch,
        FakeResolver(
            plan=lambda request: AgentRuntimePlan(
                action="inherit",
                runtime=request.baseline,
                owns_fallbacks=owns,
                reason_code="baseline_inherit",
            )
        ),
    )
    host_fallback = {"provider": "host", "model": "host-fallback"}
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "provider": baseline.provider,
            "api_mode": baseline.api_mode,
            "base_url": baseline.base_url,
            "api_key": baseline.api_key,
        },
    )

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            model=baseline.model,
            provider=baseline.provider,
            base_url=baseline.base_url,
            api_key=baseline.api_key,
            api_mode=baseline.api_mode,
            fallback_model=host_fallback,
            runtime_routing_context=_context(),
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    assert agent._runtime_fallback_authority == ("plugin" if owns else "host")
    assert agent._fallback_chain == ([] if owns else [host_fallback])
    assert agent._runtime_routing_binding.runtime.fallback_model == (
        () if owns else (host_fallback,)
    )


@pytest.mark.parametrize("owns", [False, True])
def test_inherit_fallback_ownership_controls_init_time_provider_recovery(
    monkeypatch, owns
):
    _install_resolver(
        monkeypatch,
        FakeResolver(
            plan=lambda request: AgentRuntimePlan(
                action="inherit",
                runtime=request.baseline,
                owns_fallbacks=owns,
                reason_code="baseline_inherit",
            )
        ),
    )
    calls = []
    def resolve(*, requested, target_model, **_kwargs):
        calls.append((requested, target_model))
        if requested == "host-fallback-provider":
            return {
                "provider": requested,
                "api_mode": "chat_completions",
                "base_url": "https://fallback.invalid/v1",
                "api_key": "fallback-secret",
            }
        raise RuntimeError("primary unavailable")

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider", resolve
    )
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *_a, **_k: pytest.fail(
            "two-phase resolution must produce explicit client credentials"
        ),
    )
    monkeypatch.setattr(
        AIAgent,
        "_create_openai_client",
        lambda *_a, **_k: SimpleNamespace(),
    )
    kwargs = dict(
        model="primary-model",
        provider="missing-provider",
        fallback_model={
            "provider": "host-fallback-provider",
            "model": "host-fallback-model",
        },
        runtime_routing_context=_context(),
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
    ):
        if owns:
            with pytest.raises(RuntimeError, match="explicit primary runtime"):
                AIAgent(**kwargs)
        else:
            agent = AIAgent(**kwargs)
            assert agent.provider == "host-fallback-provider"

    assert calls == (
        [("missing-provider", "primary-model")]
        if owns
        else [
            ("missing-provider", "primary-model"),
            ("host-fallback-provider", "host-fallback-model"),
        ]
    )


def test_manual_transition_records_pending_intent_without_agent(monkeypatch):
    resolver = FakeResolver()
    _install_resolver(monkeypatch, resolver)
    runtime = _projected_spec(fallback_model=())

    binding = apply_manual_runtime_transition(
        None,
        session_id="lazy-session",
        source="tui_model_command",
        runtime=runtime,
        fallback_model=(),
    )

    assert len(resolver.manual_pins) == 1
    assert resolver.manual_pins[0].session_id == "lazy-session"
    assert resolver.manual_pins[0].runtime is runtime
    assert binding.manual_pin_source == "tui_model_command"
    assert binding.session_id == "lazy-session"


def test_manual_transition_without_resolver_still_applies_host_intent(monkeypatch):
    manager = PluginManager()
    manager._discovered = True
    monkeypatch.setattr("hermes_cli.plugins._plugin_manager", manager)
    runtime = _projected_spec(fallback_model=())

    binding = apply_manual_runtime_transition(
        None,
        session_id="lazy-session",
        source="tui_model_command",
        runtime=runtime,
        fallback_model=(),
    )

    assert binding.manual_pin_source == "tui_model_command"


def test_manual_transition_persistence_error_keeps_new_local_host_intent(
    monkeypatch, caplog
):
    resolver = FakeResolver()
    resolver.record_manual_pin = lambda _request: (_ for _ in ()).throw(
        RuntimeError("token=MANUAL_PIN_SECRET")
    )
    _install_resolver(monkeypatch, resolver)
    agent = SimpleNamespace(
        _runtime_routing_binding=SimpleNamespace(action="project"),
        _runtime_fallback_authority="plugin",
        _fallback_chain=[{"provider": "old", "model": "old"}],
    )
    host_chain = [{"provider": "host", "model": "host"}]
    caplog.set_level("WARNING")

    with pytest.raises(RuntimeError, match="could not record manual transition"):
        apply_manual_runtime_transition(
            agent,
            session_id="session-a",
            source="cli_model_command",
            runtime=_projected_spec(fallback_model=()),
            fallback_model=host_chain,
        )

    assert isinstance(agent._runtime_routing_binding, RuntimeRoutingBinding)
    assert agent._runtime_routing_binding.manual_pin_source == "cli_model_command"
    assert agent._runtime_fallback_authority == "host"
    assert agent._fallback_chain == host_chain
    assert agent._fallback_model == host_chain[0]
    host_chain[0]["model"] = "mutated"
    assert agent._fallback_chain[0]["model"] == "host"
    assert "MANUAL_PIN_SECRET" not in caplog.text


def test_manual_transition_atomically_replaces_binding_and_host_fallbacks(monkeypatch):
    resolver = FakeResolver()
    _install_resolver(monkeypatch, resolver)
    agent = SimpleNamespace(
        session_id="session-a",
        _runtime_routing_binding=SimpleNamespace(action="project"),
        _runtime_fallback_authority="plugin",
        _fallback_chain=[{"provider": "plugin", "model": "old"}],
        _fallback_model={"provider": "plugin", "model": "old"},
        _fallback_index=4,
    )
    host_chain = [
        {"provider": "host-a", "model": "fallback-a", "api_key": "secret-a"},
        {"provider": "host-b", "model": "fallback-b", "api_key": "secret-b"},
    ]

    binding = apply_manual_runtime_transition(
        agent,
        session_id="session-a",
        source="cli_model_command",
        runtime=_projected_spec(fallback_model=()),
        fallback_model=host_chain,
    )

    assert agent._runtime_routing_binding is binding
    assert agent._runtime_fallback_authority == "host"
    assert agent._fallback_chain == host_chain
    assert agent._fallback_model == host_chain[0]
    assert agent._fallback_index == 0
    assert agent._fallback_activated is False
    assert "secret-a" not in repr(binding)
    host_chain[0]["model"] = "mutated-after-transition"
    assert agent._fallback_chain[0]["model"] == "fallback-a"


def test_manual_transition_persists_only_non_secret_session_runtime_metadata(
    monkeypatch,
):
    resolver = FakeResolver()
    _install_resolver(monkeypatch, resolver)
    updates = []

    class SessionDB:
        def get_session(self, session_id):
            assert session_id == "session-a"
            return {
                "model_config": json.dumps(
                    {"max_iterations": 90, "preserve_me": True}
                )
            }

        def update_session_meta(self, session_id, model_config_json, model=None):
            updates.append((session_id, json.loads(model_config_json), model))

    agent = SimpleNamespace(
        _session_db=SessionDB(),
        _session_init_model_config={"max_iterations": 90},
    )
    runtime = _projected_spec(fallback_model=())

    apply_manual_runtime_transition(
        agent,
        session_id="session-a",
        source="cli_model_command",
        runtime=runtime,
        fallback_model=(),
    )

    assert updates == [
        (
            "session-a",
            {
                "api_mode": "chat_completions",
                "base_url": "https://selected.invalid/v1",
                "max_iterations": 90,
                "model": "selected-model",
                "preserve_me": True,
                "provider": "custom:selected",
                "reasoning_config": {"effort": "high"},
                "runtime_manual_pin_source": "cli_model_command",
            },
            "selected-model",
        )
    ]
    assert agent._session_init_model_config == {
        "api_mode": "chat_completions",
        "base_url": "https://selected.invalid/v1",
        "max_iterations": 90,
        "model": "selected-model",
        "provider": "custom:selected",
        "reasoning_config": {"effort": "high"},
        "runtime_manual_pin_source": "cli_model_command",
    }
    persisted = json.dumps(updates)
    assert "selected-secret" not in persisted
    assert "ACP_TOKEN_SENTINEL" not in persisted
    assert "FALLBACK_KEY_SENTINEL" not in persisted


def test_manual_transition_omits_credential_bearing_session_base_url(monkeypatch):
    resolver = FakeResolver()
    _install_resolver(monkeypatch, resolver)
    updates = []

    class SessionDB:
        def get_session(self, _session_id):
            return {
                "model_config": json.dumps(
                    {"base_url": "https://stale.invalid/v1", "keep": "value"}
                )
            }

        def update_session_meta(self, session_id, model_config_json, model=None):
            updates.append((session_id, json.loads(model_config_json), model))

    agent = SimpleNamespace(
        _session_db=SessionDB(),
        _session_init_model_config={"base_url": "https://stale.invalid/v1"},
    )

    apply_manual_runtime_transition(
        agent,
        session_id="session-a",
        source="cli_model_command",
        runtime=_projected_spec(
            base_url=(
                "https://selected.invalid/v1/"
                "sk-proj-abcdefghijklmnopqrstuv"
            ),
            fallback_model=(),
        ),
        fallback_model=(),
    )

    assert "base_url" not in updates[0][1]
    assert "base_url" not in agent._session_init_model_config
    assert updates[0][1]["keep"] == "value"
    assert "sk-proj" not in json.dumps(updates)


@pytest.mark.parametrize("manual", [False, True])
def test_continuation_copies_routed_or_manual_binding_without_reclassification(
    monkeypatch, manual
):
    resolver = FakeResolver()
    _install_resolver(monkeypatch, resolver)
    runtime = _projected_spec(fallback_model=())
    agent = SimpleNamespace(session_id="parent")
    if manual:
        apply_manual_runtime_transition(
            agent,
            session_id="parent",
            source="cli_model_command",
            runtime=runtime,
            fallback_model=(),
        )
    else:
        resolver.plan = _project_plan(runtime)
        request = _request(context=_context(session_id="parent"))
        prepared = finalize_prepared_agent_runtime(
            prepare_agent_runtime(request), request, runtime
        )
        agent._runtime_routing_binding = prepared

    before_resolves = len(resolver.requests)
    binding = record_runtime_session_continuation(
        agent,
        parent_session_id="parent",
        child_session_id="child",
    )

    assert len(resolver.requests) == before_resolves
    assert resolver.continuations[-1].parent_session_id == "parent"
    assert resolver.continuations[-1].child_session_id == "child"
    assert binding.session_id == "child"
    assert binding.runtime is runtime
    assert binding.manual_pin_source == ("cli_model_command" if manual else None)
    assert isinstance(binding, RuntimeRoutingBinding)


def test_continuation_rejects_binding_parent_mismatch_before_persistence(monkeypatch):
    resolver = FakeResolver()
    _install_resolver(monkeypatch, resolver)
    binding = RuntimeRoutingBinding(
        scope="fresh_session",
        session_id="actual-parent",
        task_id="task-a",
        operation_id=None,
        action="inherit",
        runtime=_spec(),
        reason_code="baseline_inherit",
    )
    agent = SimpleNamespace(_runtime_routing_binding=binding)

    with pytest.raises(RuntimeContinuationError, match="parent"):
        record_runtime_session_continuation(
            agent,
            parent_session_id="wrong-parent",
            child_session_id="child",
        )

    assert resolver.continuations == []
    assert agent._runtime_routing_binding is binding


def test_continuation_repair_follows_lineage_oldest_first_and_rejects_cycles(monkeypatch):
    resolver = FakeResolver()
    _install_resolver(monkeypatch, resolver)
    parents = {"grandchild": "child", "child": "parent", "parent": None}

    repaired = repair_runtime_session_continuations(
        "grandchild", parent_session_id_for=parents.get
    )

    assert repaired == 2
    assert [
        (item.parent_session_id, item.child_session_id)
        for item in resolver.continuations
    ] == [("parent", "child"), ("child", "grandchild")]

    with pytest.raises(RuntimeContinuationError, match="cycle"):
        repair_runtime_session_continuations(
            "a", parent_session_id_for={"a": "b", "b": "a"}.get
        )


def test_store_backed_continuation_repair_uses_only_compression_lineage(monkeypatch):
    from agent.runtime_routing import (
        repair_runtime_session_continuations_from_store,
    )

    resolver = FakeResolver()
    _install_resolver(monkeypatch, resolver)

    class SessionStore:
        def get_compression_lineage(self, session_id):
            assert session_id == "grandchild"
            return ["parent", "child", "grandchild"]

    repaired = repair_runtime_session_continuations_from_store(
        "grandchild",
        session_store=SessionStore(),
    )

    assert repaired == 2
    assert [
        (item.parent_session_id, item.child_session_id)
        for item in resolver.continuations
    ] == [("parent", "child"), ("child", "grandchild")]


def test_store_backed_continuation_repair_rejects_non_lineage_and_wrong_tip(
    monkeypatch,
):
    from agent.runtime_routing import (
        repair_runtime_session_continuations_from_store,
    )

    resolver = FakeResolver()
    _install_resolver(monkeypatch, resolver)

    class BranchStore:
        def get_compression_lineage(self, _session_id):
            return ["branch"]

    class WrongTipStore:
        def get_compression_lineage(self, _session_id):
            return ["parent", "different-tip"]

    assert (
        repair_runtime_session_continuations_from_store(
            "branch", session_store=BranchStore()
        )
        == 0
    )
    with pytest.raises(RuntimeContinuationError, match="requested session"):
        repair_runtime_session_continuations_from_store(
            "requested", session_store=WrongTipStore()
        )
    assert resolver.continuations == []


def test_construction_preparation_repairs_resume_before_resolver_read(monkeypatch):
    from agent.runtime_routing import prepare_agent_runtime_for_construction

    events = []

    class OrderedResolver(FakeResolver):
        def record_session_continuation(self, request):
            events.append(
                ("repair", request.parent_session_id, request.child_session_id)
            )

        def resolve(self, request):
            events.append(("resolve", request.context.session_id))
            return AgentRuntimePlan(
                action="inherit",
                runtime=request.baseline,
                reason_code="baseline_inherit",
            )

    class SessionStore:
        def get_compression_lineage(self, _session_id):
            return ["parent", "child"]

    _install_resolver(monkeypatch, OrderedResolver())
    request = _request(context=_context(session_id="child", is_resume=True))

    prepare_agent_runtime_for_construction(
        request,
        session_store=SessionStore(),
    )

    assert events == [
        ("repair", "parent", "child"),
        ("resolve", "child"),
    ]


def test_continuation_repair_rejects_unbounded_depth(monkeypatch):
    _install_resolver(monkeypatch, FakeResolver())
    parents = {"d": "c", "c": "b", "b": "a", "a": None}

    with pytest.raises(RuntimeContinuationError, match="maximum depth"):
        repair_runtime_session_continuations(
            "d", parent_session_id_for=parents.get, max_depth=2
        )


def test_optional_continuation_callback_degrades_to_binding_copy_and_no_repair(
    monkeypatch,
):
    class MinimalResolver:
        def requires_initial_task(self, _scope):
            return False

        def resolve(self, request):
            return AgentRuntimePlan(
                action="inherit",
                runtime=request.baseline,
                reason_code="baseline_inherit",
            )

        def record_manual_pin(self, _request):
            return None

    resolver = MinimalResolver()
    _install_resolver(monkeypatch, resolver)
    binding = RuntimeRoutingBinding(
        scope="fresh_session",
        session_id="parent",
        task_id="task-a",
        operation_id=None,
        action="inherit",
        runtime=_spec(),
        reason_code="baseline_inherit",
    )
    agent = SimpleNamespace(_runtime_routing_binding=binding)

    continued = record_runtime_session_continuation(
        agent, parent_session_id="parent", child_session_id="child"
    )

    assert continued.session_id == "child"
    assert continued.runtime is binding.runtime
    assert (
        repair_runtime_session_continuations(
            "child", parent_session_id_for={"child": "parent", "parent": None}.get
        )
        == 0
    )


def test_repair_replay_is_deterministic_for_idempotent_resolver(monkeypatch):
    class IdempotentResolver(FakeResolver):
        def __init__(self):
            super().__init__()
            self.aliases = set()

        def record_session_continuation(self, request):
            self.aliases.add((request.parent_session_id, request.child_session_id))

    resolver = IdempotentResolver()
    _install_resolver(monkeypatch, resolver)
    parents = {"grandchild": "child", "child": "parent", "parent": None}

    assert repair_runtime_session_continuations(
        "grandchild", parent_session_id_for=parents.get
    ) == 2
    assert repair_runtime_session_continuations(
        "grandchild", parent_session_id_for=parents.get
    ) == 2
    assert resolver.aliases == {("parent", "child"), ("child", "grandchild")}
