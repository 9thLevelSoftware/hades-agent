"""Registration contracts for the opt-in auto-routing plugin shell."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from utils import fast_safe_load
from plugins.auto_routing.auto_routing.models import RoutingTarget


def test_auto_routing_plugin_registers_cli_skill_resolver_and_epoch_observer(
    plugin_context,
    load_bundled_plugin,
):
    module = load_bundled_plugin("auto_routing")

    module.register(plugin_context)

    assert plugin_context.cli_commands == ["auto-routing"]
    assert plugin_context.skills == ["auto-routing:auto-routing"]
    assert plugin_context.tools == []
    assert plugin_context.middleware == []
    assert plugin_context.hooks == ["pre_api_request", "post_turn_outcome"]
    assert plugin_context._manager.agent_runtime_resolver is not None
    assert plugin_context._manager.agent_runtime_resolver_owner == "auto-routing"
    assert plugin_context._manager.agent_runtime_resolver.requires_initial_task(
        "fresh_session"
    ) is True


def test_auto_routing_skill_requires_explicit_capability_collection() -> None:
    skill = (
        Path(__file__).resolve().parents[3]
        / "plugins"
        / "auto_routing"
        / "skills"
        / "auto-routing"
        / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "required capabilities" in skill
    assert "including an explicit empty list" in skill


def test_stage_two_status_reports_available_projection_without_activating(service, capsys):
    status = service.status()

    assert status["runtime_projection"] == "available"
    assert status["activation_mode"] in {"off", "shadow"}
    assert capsys.readouterr().out == ""


def test_stage_one_cli_reports_status(service, load_bundled_plugin, capsys):
    module = load_bundled_plugin("auto_routing")
    parser = argparse.ArgumentParser()
    module.build_parser(parser)

    args = parser.parse_args([])
    result = module.auto_routing_command(args, service=service)

    assert result == 0
    assert json.loads(capsys.readouterr().out) == service.status()


def test_stage3_docs_publish_feedback_report_and_non_adaptive_boundary():
    repo_root = Path(__file__).resolve().parents[3]
    readme = (repo_root / "plugins/auto_routing/README.md").read_text(
        encoding="utf-8"
    )
    skill = (
        repo_root / "plugins/auto_routing/skills/auto-routing/SKILL.md"
    ).read_text(encoding="utf-8")
    manifest = fast_safe_load(
        (repo_root / "plugins/auto_routing/plugin.yaml").read_bytes()
    )
    folded = readme.casefold()
    normalized = " ".join(folded.split())

    assert "auto-routing feedback --evidence-id" in readme
    assert "auto-routing report" in readme
    assert "descriptive" in folded
    assert "no adaptive writes" in folded
    assert "never infer feedback" in skill.casefold()
    assert manifest["hooks"] == ["pre_api_request", "post_turn_outcome"]
    assert manifest["version"] == "0.3.0"

    for quality_unknown in (
        "completed_unverified",
        "partial",
        "blocked",
        "failed",
        "interrupted",
        "unresolved",
        "cancelled",
    ):
        assert quality_unknown in readme

    assert "continuation context is unavailable" in normalized
    assert "latency availability is nullable" in normalized
    assert "append-only" in folded
    assert "read-only" in folded
    assert "no decision-population denominator" in normalized
    for excluded in (
        "moa",
        "judges",
        "canaries",
        "rankings",
        "recommendations",
        "outbound telemetry",
    ):
        assert excluded in folded


def test_stage4_docs_publish_profile_local_adaptation_controls_and_exclusions():
    repo_root = Path(__file__).resolve().parents[3]
    readme = (repo_root / "plugins/auto_routing/README.md").read_text(
        encoding="utf-8"
    )
    skill = (
        repo_root / "plugins/auto_routing/skills/auto-routing/SKILL.md"
    ).read_text(encoding="utf-8")
    manifest = fast_safe_load(
        (repo_root / "plugins/auto_routing/plugin.yaml").read_bytes()
    )
    folded = f"{readme}\n{skill}".casefold()

    for command in (
        "adapt status --profile-id",
        "adapt history --profile-id",
        "adapt freeze --profile-id",
        "adapt unfreeze --profile-id",
        "adapt rollback --profile-id",
    ):
        assert command in folded
    for concept in (
        "opt-in per profile",
        "verified outcomes",
        "explicit feedback",
        "freeze",
        "deterministic",
        "provider discovery",
        "fallback mutation",
        "classifier",
        "evaluator",
        "outbound telemetry",
        "moa",
    ):
        assert concept in folded
    assert manifest["version"] == "0.3.0"
    assert manifest["hooks"] == ["pre_api_request", "post_turn_outcome"]


def test_stage4_readme_profile_targets_are_valid_typed_examples() -> None:
    readme = (
        Path(__file__).resolve().parents[3] / "plugins/auto_routing/README.md"
    ).read_text(encoding="utf-8")
    match = re.search(r"```yaml\n(?P<document>.*?)\n```", readme, re.DOTALL)
    assert match is not None
    document = fast_safe_load(match.group("document"))
    profile = document["profiles"]["coding"]

    assert RoutingTarget.model_validate(profile["primary"])
    assert RoutingTarget.model_validate(profile["primary_challengers"][0])
