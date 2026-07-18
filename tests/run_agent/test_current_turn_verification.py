"""Current-turn verification status is produced from tool evidence."""

import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from run_agent import AIAgent


def _response(content="", *, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="tool_calls" if tool_calls else "stop")],
        model="test/model",
        usage=None,
    )


def _tool_call(call_id):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name="terminal", arguments='{"command":"scripts/run_tests.sh"}'),
    )


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    with (
        patch(
            "run_agent.get_tool_definitions",
            return_value=[
                {
                    "type": "function",
                    "function": {
                        "name": "terminal",
                        "description": "terminal",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        instance = AIAgent(
            session_id="verification-status-test",
            api_key="test-key",
            base_url="https://example.invalid/v1",
            provider="openai-compat",
            model="test/model",
            max_iterations=3,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    instance._cached_system_prompt = "stable test prompt"
    instance._session_db = None
    instance._session_json_enabled = False
    instance.save_trajectories = False
    instance.compression_enabled = False
    instance._cleanup_task_resources = lambda *_a, **_kw: None
    instance._save_trajectory = lambda *_a, **_kw: None
    return instance


def _run(agent, responses, *, tool_result=None):
    agent._interruptible_api_call = lambda _kwargs: next(responses)
    patches = [
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
        patch.object(agent, "_persist_session"),
    ]
    if tool_result is not None:
        patches.append(patch("run_agent.handle_function_call", return_value=tool_result))
    with ExitStack() as stack:
        for context in patches:
            stack.enter_context(context)
        return agent.run_conversation("do it")


def test_turn_boundary_resets_stale_verification_status(agent):
    agent._turn_verification_status = {"status": "passed"}

    result = _run(agent, iter([_response("plain answer")]))

    assert result["outcome"] == "completed_unverified"
    assert result["completed"] is True


def test_no_tool_turn_is_completed_unverified(agent):
    result = _run(agent, iter([_response("plain answer")]))

    assert result["outcome"] == "completed_unverified"
    assert result["completed"] is True


def test_passed_terminal_evidence_produces_verified_outcome(agent):
    evidence_result = json.dumps(
        {
            "output": "1 passed",
            "exit_code": 0,
            "error": None,
            "verification_evidence": {
                "status": "passed",
                "kind": "test",
                "scope": "repository",
                "canonical_command": "scripts/run_tests.sh",
            },
        }
    )
    result = _run(
        agent,
        iter([_response(tool_calls=[_tool_call("c1")]), _response("verified answer")]),
        tool_result=evidence_result,
    )

    assert result["outcome"] == "verified"
    assert result["completed"] is True


def test_failed_terminal_evidence_never_verifies(agent):
    failed_result = json.dumps(
        {
            "output": "1 failed",
            "exit_code": 1,
            "error": None,
            "verification_evidence": {
                "status": "failed",
                "kind": "test",
                "scope": "repository",
                "canonical_command": "scripts/run_tests.sh",
            },
        }
    )
    result = _run(
        agent,
        iter([_response(tool_calls=[_tool_call("c1")]), _response("answer")]),
        tool_result=failed_result,
    )

    assert result["outcome"] == "completed_unverified"
    assert result["completed"] is True
