"""Adversarial Stage 4 adaptation control and dependency boundaries."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import httpx
import pytest

from plugins.auto_routing.auto_routing.service import AutoRoutingServiceError
from tests.plugins.auto_routing.test_adaptation_cli import adaptation_service


def test_adaptation_lifecycle_uses_no_network(adaptation_service, monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        pytest.fail("adaptation attempted outbound network access")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(httpx.Client, "request", forbidden)

    result = adaptation_service.maybe_advance_adaptation(
        profile_id="coding",
        now="2026-07-18T12:00:00Z",
    )

    assert result.action == "hold"


def test_adaptation_modules_keep_closed_import_boundaries() -> None:
    root = Path(__file__).resolve().parents[3] / "plugins" / "auto_routing" / "auto_routing"
    selector = (root / "selector.py").read_text(encoding="utf-8").casefold()
    adaptation = (root / "adaptation.py").read_text(encoding="utf-8").casefold()
    learner = (root / "learner.py").read_text(encoding="utf-8").casefold()

    assert "from .learner" not in selector
    assert "evidence_events" not in selector
    assert "evidenceevent" not in selector
    for source in (adaptation, learner):
        for forbidden in (
            "import httpx",
            "import requests",
            "import socket",
            "provider",
            "mixture_of_agents",
            "from .classifier",
            "from .evaluator",
            "telemetry",
        ):
            assert forbidden not in source


def test_v7_adaptation_schema_and_control_output_are_content_free(
    adaptation_service,
) -> None:
    from tests.plugins.auto_routing.test_adaptation_cli import _publish_revisions

    _publish_revisions(adaptation_service)
    status = adaptation_service.adaptation_status("coding")
    history = adaptation_service.adaptation_history("coding")
    output = json.dumps({"status": status, "history": history}, sort_keys=True).casefold()
    schema = " ".join(
        str(row[0])
        for row in adaptation_service.store.connection.execute(
            "SELECT sql FROM sqlite_master WHERE name LIKE 'adaptive_%'"
        ).fetchall()
    ).casefold()

    for forbidden in (
        "prompt_body",
        "response_body",
        "endpoint_url",
        "api_key",
        "credential_value",
        "authorization_header",
    ):
        assert forbidden not in output
        assert forbidden not in schema


def test_control_operations_never_change_yaml_or_fallback_authority(
    adaptation_service,
) -> None:
    from tests.plugins.auto_routing.test_adaptation_cli import _publish_revisions

    revision_a, _revision_b = _publish_revisions(adaptation_service)
    config_before = adaptation_service.config_path.read_bytes()
    authority_before = adaptation_service._configured_authority()
    fallbacks_before = {
        profile_id: tuple(
            target.model_dump_json()
            for target in profile.fallbacks
        )
        for profile_id, profile in authority_before.profiles.items()
    }
    freeze = adaptation_service.preview_adaptation_control(
        action="freeze",
        profile_id="coding",
    )
    adaptation_service.apply_adaptation_control(
        action="freeze",
        profile_id="coding",
        expected_hash=freeze["precondition_hash"],
    )
    rollback = adaptation_service.preview_adaptation_control(
        action="rollback",
        profile_id="coding",
        revision_id=revision_a,
    )
    adaptation_service.apply_adaptation_control(
        action="rollback",
        profile_id="coding",
        revision_id=revision_a,
        expected_hash=rollback["precondition_hash"],
    )

    authority_after = adaptation_service._configured_authority()
    fallbacks_after = {
        profile_id: tuple(target.model_dump_json() for target in profile.fallbacks)
        for profile_id, profile in authority_after.profiles.items()
    }
    assert adaptation_service.config_path.read_bytes() == config_before
    assert fallbacks_after == fallbacks_before


def test_cross_profile_revision_cannot_be_previewed_for_rollback(
    adaptation_service,
) -> None:
    from plugins.auto_routing.auto_routing.config import authority_revision
    from plugins.auto_routing.auto_routing.models import (
        AdaptiveOverlay,
        AdaptiveProfileRevision,
    )

    config = adaptation_service._configured_authority()
    authority_id = authority_revision(config)
    foreign = AdaptiveProfileRevision(
        revision_id="foreign-revision",
        authority_id=authority_id,
        profile_id="research",
        overlay=AdaptiveOverlay(
            profile_id="research",
            ordered_primary_runtime_ids=("f" * 64,),
        ),
        lifecycle="eligible",
        created_at="2026-07-18T12:00:00Z",
    )
    adaptation_service.store.publish_profile_revision(
        foreign,
        expected_revision_id=None,
        expected_generation=0,
    )

    with pytest.raises(AutoRoutingServiceError, match="profile|revision"):
        adaptation_service.preview_adaptation_control(
            action="rollback",
            profile_id="coding",
            revision_id=foreign.revision_id,
        )
