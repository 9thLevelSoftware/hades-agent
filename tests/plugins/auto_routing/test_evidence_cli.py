"""Finite, append-only route feedback service and CLI contracts."""

from __future__ import annotations

import argparse
import json

import pytest

from plugins.auto_routing.auto_routing.cli import (
    CommandWriteClass,
    build_parser,
    command_metadata,
    execute,
)
from plugins.auto_routing.auto_routing.service import AutoRoutingServiceError


FEEDBACK_VALUES = (
    "rating-1",
    "rating-2",
    "rating-3",
    "rating-4",
    "rating-5",
    "rejected",
    "corrected",
    "manual-reroute",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    build_parser(parser)
    return parser


def _assert_no_ranking_keys(value):
    forbidden = {
        "winner",
        "recommendation",
        "ranking",
        "rank",
        "score",
        "best_model",
    }
    if isinstance(value, dict):
        assert forbidden.isdisjoint(value)
        for child in value.values():
            _assert_no_ranking_keys(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_ranking_keys(child)


def test_feedback_command_is_append_only_observation():
    assert (
        command_metadata("feedback").write_class
        is CommandWriteClass.APPEND_ONLY_OBSERVATION
    )


@pytest.mark.parametrize("value", FEEDBACK_VALUES)
def test_feedback_accepts_only_finite_values(active_route, value):
    routed_turn_event = active_route.service.ingest_turn_outcome(
        active_route.payload()
    ).event
    args = _parser().parse_args(
        [
            "feedback",
            "--evidence-id",
            routed_turn_event.evidence_id,
            "--value",
            value,
            "--json",
        ]
    )
    result = execute(args, service=active_route.service)
    assert result.exit_code == 0
    assert result.payload["write_class"] == "append_only_observation"
    assert result.payload["feedback_value"] == value


def test_identical_feedback_replays_but_different_value_is_preserved(active_route):
    routed_turn_event = active_route.service.ingest_turn_outcome(
        active_route.payload()
    ).event
    first = active_route.service.record_feedback(
        evidence_id=routed_turn_event.evidence_id,
        value="rating-3",
    )
    replay = active_route.service.record_feedback(
        evidence_id=routed_turn_event.evidence_id,
        value="rating-3",
    )
    changed = active_route.service.record_feedback(
        evidence_id=routed_turn_event.evidence_id,
        value="rating-4",
    )
    assert first["status"] == "inserted"
    assert replay["status"] == "replayed"
    assert changed["status"] == "inserted"
    assert replay["observed_at"] == first["observed_at"]
    children = active_route.service.store.list_evidence_events(
        parent_evidence_id=routed_turn_event.evidence_id
    )
    assert {event.feedback_value for event in children} == {
        "rating-3",
        "rating-4",
    }


def test_feedback_rejects_unknown_parent_or_feedback_parent(active_route):
    with pytest.raises(AutoRoutingServiceError, match="turn evidence"):
        active_route.service.record_feedback(
            evidence_id="0" * 64,
            value="rating-5",
        )
    routed_turn_event = active_route.service.ingest_turn_outcome(
        active_route.payload()
    ).event
    first = active_route.service.record_feedback(
        evidence_id=routed_turn_event.evidence_id,
        value="rating-5",
    )
    with pytest.raises(AutoRoutingServiceError, match="turn evidence"):
        active_route.service.record_feedback(
            evidence_id=first["evidence_id"],
            value="rating-1",
        )


@pytest.mark.parametrize(
    ("forbidden_flag", "forbidden_value"),
    (
        ("--comment", "raw user text"),
        ("--observed-at", "2026-07-18T12:00:00Z"),
    ),
)
def test_feedback_parser_has_no_free_text_or_client_timestamp(
    forbidden_flag,
    forbidden_value,
):
    with pytest.raises(SystemExit):
        _parser().parse_args(
            [
                "feedback",
                "--evidence-id",
                "0" * 64,
                "--value",
                "rating-5",
                forbidden_flag,
                forbidden_value,
            ]
        )


def test_feedback_does_not_mutate_routing_control_plane(active_route):
    service = active_route.service
    routed_turn_event = service.ingest_turn_outcome(active_route.payload()).event
    before = (
        tuple(service.store.list_authority_revisions()),
        service.store.read_decision(active_route.decision.decision_id),
        service.store.read_session_binding(active_route.session_id),
        service.store.read_route_epochs(active_route.session_id),
    )

    service.record_feedback(
        evidence_id=routed_turn_event.evidence_id,
        value="corrected",
    )

    after = (
        tuple(service.store.list_authority_revisions()),
        service.store.read_decision(active_route.decision.decision_id),
        service.store.read_session_binding(active_route.session_id),
        service.store.read_route_epochs(active_route.session_id),
    )
    assert after == before


def test_report_command_is_read_only_and_has_closed_filter_grammar():
    assert command_metadata("report").write_class is CommandWriteClass.READ_ONLY
    args = _parser().parse_args(
        [
            "report",
            "--days",
            "14",
            "--decision-id",
            "decision-a",
            "--profile-id",
            "coding",
            "--runtime-id",
            "a" * 64,
            "--reasoning-effort",
            "high",
            "--json",
        ]
    )
    assert vars(args) == {
        "auto_routing_action": "report",
        "auto_routing_json": False,
        "days": 14,
        "decision_id": "decision-a",
        "profile_id": "coding",
        "runtime_id": "a" * 64,
        "reasoning_effort": "high",
        "json": True,
    }
    with pytest.raises(SystemExit):
        _parser().parse_args(["report", "--provider", "openrouter"])


def test_report_is_read_only_descriptive_and_separates_continuations(
    active_route,
):
    initial_event = active_route.service.ingest_turn_outcome(
        active_route.payload()
    ).event
    active_route.service.record_feedback(
        evidence_id=initial_event.evidence_id,
        value="rating-5",
    )
    child = active_route.compression_child(
        child_session_id="report-compressed-child",
        child_task_id="report-compressed-task",
    )
    continuation_event = child.service.ingest_turn_outcome(
        child.payload(outcome="completed_unverified", api_calls=2)
    ).event
    before_changes = active_route.service.store.connection.total_changes
    before_config = active_route.service.config_path.read_bytes()
    decision = active_route.service.store.read_decision(initial_event.decision_id)
    assert decision is not None
    before_adaptive = active_route.service.store.read_active_revision(
        decision.authority_revision
    )

    report = active_route.service.report(days=30)

    assert active_route.service.store.connection.total_changes == before_changes
    assert active_route.service.config_path.read_bytes() == before_config
    assert (
        active_route.service.store.read_active_revision(decision.authority_revision)
        == before_adaptive
    )
    assert report["descriptive_only"] is True
    assert report["observations"] == {
        "turn_events": 2,
        "feedback_events": 1,
        "quality_unknown_events": 1,
        "initial_routing_task_events": 1,
        "continuation_events": 1,
        "latency_observed_events": 0,
    }
    assert "decisions_by_projection" not in json.dumps(report)
    _assert_no_ranking_keys(report)
    assert sum(group["operations"]["api_calls"] for group in report["groups"]) == 3
    assert sum(group["feedback"]["rating-5"] for group in report["groups"]) == 1
    continuation_group = next(
        group
        for group in report["groups"]
        if not group["is_initial_routing_task"]
    )
    assert continuation_group["context_bucket"] is None
    assert continuation_group["outcomes"][continuation_event.outcome] == 1
    assert "continuation_context_unavailable" in report["warnings"]
    assert "latency_unavailable" in report["warnings"]
    assert "quality_unknown_present" in report["warnings"]


def test_report_filters_exact_runtime_and_reasoning(active_route):
    high_event = active_route.service.ingest_turn_outcome(
        active_route.payload()
    ).event
    report = active_route.service.report(
        days=30,
        runtime_id=high_event.runtime_id,
        reasoning_effort="high",
    )
    assert {group["runtime_id"] for group in report["groups"]} == {
        high_event.runtime_id
    }
    assert {group["reasoning_effort"] for group in report["groups"]} == {"high"}
    assert (
        active_route.service.report(
            days=30,
            runtime_id=high_event.runtime_id,
            reasoning_effort="low",
        )["groups"]
        == []
    )


def test_report_cli_dispatches_exact_filters(active_route):
    event = active_route.service.ingest_turn_outcome(active_route.payload()).event
    result = execute(
        _parser().parse_args(
            [
                "report",
                "--days",
                "7",
                "--decision-id",
                event.decision_id,
                "--profile-id",
                event.profile_id,
                "--runtime-id",
                event.runtime_id,
                "--reasoning-effort",
                event.reasoning_effort,
                "--json",
            ]
        ),
        service=active_route.service,
    )
    assert result.exit_code == 0
    assert result.payload["write_class"] == "read_only"
    assert result.payload["window"]["days"] == 7
    assert result.payload["filters"] == {
        "decision_id": event.decision_id,
        "profile_id": event.profile_id,
        "runtime_id": event.runtime_id,
        "reasoning_effort": event.reasoning_effort,
    }


def test_report_groups_are_deterministic_and_feedback_can_contradict(active_route):
    parent = active_route.service.ingest_turn_outcome(active_route.payload()).event
    for value in ("rejected", "corrected"):
        active_route.service.record_feedback(
            evidence_id=parent.evidence_id,
            value=value,
        )
    child = active_route.compression_child(
        child_session_id="sorted-child",
        child_task_id="sorted-child-task",
    )
    child.service.ingest_turn_outcome(child.payload())

    first = active_route.service.report(days=30)
    second = active_route.service.report(days=30)

    assert first["groups"] == second["groups"]
    keys = [
        (
            group["profile_id"],
            group["runtime_id"],
            group["reasoning_effort"],
            group["is_initial_routing_task"],
            None
            if group["context_bucket"] is None
            else group["context_bucket"]["bucket_id"],
        )
        for group in first["groups"]
    ]
    assert keys == sorted(
        keys,
        key=lambda key: tuple("" if value is None else str(value) for value in key),
    )
    assert "contradictory_feedback_present" in first["warnings"]


@pytest.mark.parametrize("days", (0, -1, 3651, True))
def test_report_rejects_unbounded_window(active_route, days):
    with pytest.raises(AutoRoutingServiceError, match="days"):
        active_route.service.report(days=days)


def test_explain_lists_content_free_evidence_ids(active_route):
    routed_turn_event = active_route.service.ingest_turn_outcome(
        active_route.payload()
    ).event
    concise = active_route.service.explain(
        decision_id=routed_turn_event.decision_id
    )
    detailed = active_route.service.explain(
        decision_id=routed_turn_event.decision_id,
        detailed=True,
    )
    assert concise["evidence"] == {
        "event_ids": [routed_turn_event.evidence_id],
        "turn_outcomes": 1,
        "explicit_feedback": 0,
        "quality_unknown": 0,
    }
    assert detailed["evidence_events"][0]["turn_id"] == routed_turn_event.turn_id
    assert "prompt" not in json.dumps(detailed).casefold()
