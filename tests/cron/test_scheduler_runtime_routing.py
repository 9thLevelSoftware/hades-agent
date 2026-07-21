from __future__ import annotations


def test_cron_runtime_context_uses_known_prompt_and_stable_execution_ids():
    from cron.scheduler import _build_cron_runtime_context

    prompt = "Review open incidents and report only actionable changes."
    context = _build_cron_runtime_context(
        {"id": "job-17", "name": "incident review"},
        prompt=prompt,
        session_id="cron_job-17_20260717_120000",
    )

    assert context.scope == "fresh_session"
    assert context.task == prompt
    assert context.session_id == "cron_job-17_20260717_120000"
    assert context.task_id.startswith("cron-task-")
    assert context.manual_runtime_pin is False
    assert context.manual_pin_source is None
    assert context.metadata == {"platform": "cron"}
    assert prompt not in context.task_id


def test_explicit_cron_model_or_provider_is_manual_intent():
    from cron.scheduler import _build_cron_runtime_context

    for job in (
        {"id": "model-pin", "model": "manual-model"},
        {"id": "provider-pin", "provider": "manual-provider"},
    ):
        context = _build_cron_runtime_context(
            job,
            prompt="first task",
            session_id=f"cron_{job['id']}_20260717_120000",
        )
        assert context.manual_runtime_pin is True
        assert context.manual_pin_source == "cron_job"


def test_active_cron_route_precedes_unavailable_baseline(monkeypatch, tmp_path):
    import agent.runtime_routing as routing
    import cron.scheduler as scheduler
    import hermes_state
    import run_agent

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(scheduler, "_build_job_prompt", lambda job, **_kw: job["prompt"])
    monkeypatch.setattr(scheduler, "_resolve_delivery_target", lambda _job: None)
    monkeypatch.setattr(scheduler, "_guard_job_credential_exfil", lambda _job: None)
    monkeypatch.setattr(scheduler, "_teardown_cron_agent", lambda *_args: None)

    class FakeSessionDB:
        def set_session_title(self, *_args):
            pass

        def end_session(self, *_args):
            pass

        def close(self):
            pass

    monkeypatch.setattr(hermes_state, "SessionDB", FakeSessionDB)

    from hermes_cli import env_loader
    from tools import mcp_tool

    monkeypatch.setattr(env_loader, "reset_secret_source_cache", lambda: None)
    monkeypatch.setattr(env_loader, "load_hermes_dotenv", lambda **_kw: None)
    monkeypatch.setattr(mcp_tool, "discover_mcp_tools", lambda: [])

    target = routing.AgentRuntimeSpec(
        model="selected-model",
        provider="openrouter",
        base_url="https://selected.invalid/v1",
        api_key="selected-key",
        resolution_state="resolved",
        api_mode="chat_completions",
    )
    captured_request = []

    def fake_prepare(request):
        captured_request.append(request)
        return routing._new_prepared(
            request,
            routing.AgentRuntimePlan(
                action="project",
                runtime=target,
                decision_id="decision-cron-1",
                bound_route_identity="openrouter/selected-model",
                owns_fallbacks=True,
                reason_code="active_projected",
            ),
        )

    monkeypatch.setattr(routing, "prepare_agent_runtime", fake_prepare)
    monkeypatch.setattr(
        routing,
        "runtime_resolver_requires_initial_task",
        lambda _scope: True,
    )
    baseline_attempts = []

    def unavailable_baseline(_requested_runtime):
        baseline_attempts.append(True)
        raise RuntimeError("configured baseline unavailable")

    monkeypatch.setattr(
        scheduler,
        "_resolve_cron_canonical_runtime",
        unavailable_baseline,
    )

    constructed = []

    class FakeAgent:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        @staticmethod
        def _format_turn_completion_explanation(_reason):
            return ""

        def run_conversation(self, prompt, task_id=None):
            self.prompt = prompt
            self.task_id = task_id
            return {
                "final_response": "selected response",
                "completed": True,
                "failed": False,
            }

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)

    success, _doc, response, error = scheduler.run_job(
        {
            "id": "cron-auto-route",
            "name": "Auto route",
            "prompt": "solve the first task",
        }
    )

    assert success is True
    assert response == "selected response"
    assert error is None
    assert baseline_attempts == [True]
    assert len(captured_request) == 1
    assert len(constructed) == 1
    kwargs = constructed[0]
    assert kwargs["model"] == "selected-model"
    assert kwargs["provider"] == "openrouter"
    assert kwargs["runtime_routing_context"].task == "solve the first task"
    assert kwargs["prepared_agent_runtime"].plan.decision_id == "decision-cron-1"
