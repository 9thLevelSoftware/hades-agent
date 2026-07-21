from __future__ import annotations


def _config(**overrides):
    config = {
        "distribution": "default",
        "model": "configured-baseline",
        "max_iterations": 7,
        "run_name": "routing regression run",
    }
    config.update(overrides)
    return config


def test_batch_prompt_constructs_fresh_session_with_clean_first_task(monkeypatch):
    import batch_runner

    constructed = []

    class FakeAgent:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        def run_conversation(self, prompt, task_id=None):
            return {
                "messages": [],
                "completed": True,
                "partial": False,
                "api_calls": 0,
            }

        def _convert_to_trajectory_format(self, messages, prompt, completed):
            return [{"from": "human", "value": prompt}]

    monkeypatch.setattr(batch_runner, "AIAgent", FakeAgent)
    monkeypatch.setattr(
        batch_runner,
        "sample_toolsets_from_distribution",
        lambda _distribution: [],
    )
    monkeypatch.setattr(
        batch_runner,
        "runtime_resolver_requires_initial_task",
        lambda _scope: True,
    )

    prompt = "Fix the race without changing the public API."
    result = batch_runner._process_single_prompt(
        12,
        {"prompt": prompt},
        3,
        _config(),
    )

    assert result["success"] is True
    assert len(constructed) == 1
    kwargs = constructed[0]
    context = kwargs["runtime_routing_context"]
    assert context.scope == "fresh_session"
    assert context.task == prompt
    assert context.task_id == "task_12"
    assert context.session_id == kwargs["session_id"]
    assert context.manual_runtime_pin is False
    assert context.metadata == {"platform": "batch"}
    assert prompt not in context.session_id


def test_batch_session_identity_is_stable_per_run_and_prompt():
    from batch_runner import _batch_session_id

    assert _batch_session_id("nightly run", 9) == _batch_session_id("nightly run", 9)
    assert _batch_session_id("nightly run", 9) != _batch_session_id("nightly run", 10)
    assert _batch_session_id("nightly run", 9) != _batch_session_id("other run", 9)
    assert "nightly" not in _batch_session_id("nightly run", 9)


def test_batch_without_registered_resolver_preserves_ordinary_constructor(monkeypatch):
    import batch_runner

    constructed = []

    class FakeAgent:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        def run_conversation(self, _prompt, task_id=None):
            return {"messages": [], "completed": True, "api_calls": 0}

        def _convert_to_trajectory_format(self, *_args):
            return [{"from": "human", "value": "ordinary"}]

    monkeypatch.setattr(batch_runner, "AIAgent", FakeAgent)
    monkeypatch.setattr(
        batch_runner,
        "sample_toolsets_from_distribution",
        lambda _distribution: [],
    )
    monkeypatch.setattr(
        batch_runner,
        "runtime_resolver_requires_initial_task",
        lambda _scope: False,
    )

    result = batch_runner._process_single_prompt(
        1,
        {"prompt": "ordinary"},
        0,
        _config(),
    )

    assert result["success"] is True
    assert constructed[0]["runtime_routing_context"] is None
