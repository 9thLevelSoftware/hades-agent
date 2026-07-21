"""Pure materialization contracts for profile-local adaptive overlays."""

from __future__ import annotations

import copy

import pytest

from plugins.auto_routing.auto_routing.config import parse_config
from plugins.auto_routing.auto_routing.models import AdaptiveOverlay
from test_models_config import _VALID_ROOT


@pytest.fixture
def config():
    root = copy.deepcopy(_VALID_ROOT)
    plugin = root["plugins"]["entries"]["auto-routing"]
    profile = plugin["profiles"]["coding"]
    challenger = copy.deepcopy(profile["primary"])
    challenger["runtime"]["model"] = "gpt-5.4-challenger"
    challenger["revision_status"] = "challenger"
    fallback = copy.deepcopy(profile["primary"])
    fallback["runtime"]["model"] = "gpt-5.4-mini"
    fallback["revision_status"] = "fallback"
    profile["primary_challengers"] = [challenger]
    profile["fallbacks"] = [fallback]
    return parse_config(root)


def _overlay(config, *, defaults: dict[str, str] | None = None) -> AdaptiveOverlay:
    profile = config.profiles["coding"]
    return AdaptiveOverlay(
        profile_id="coding",
        ordered_primary_runtime_ids=tuple(
            target.runtime.stable_id() for target in reversed(profile.primary_choices())
        ),
        reasoning_defaults=defaults or {},
    )


def test_overlay_is_exact_primary_permutation(config) -> None:
    from plugins.auto_routing.auto_routing.adaptation import validate_overlay

    overlay = _overlay(config)

    assert validate_overlay(config, overlay) == overlay


def test_overlay_rejects_fallback(config) -> None:
    from plugins.auto_routing.auto_routing.adaptation import validate_overlay

    fallback = config.profiles["coding"].fallbacks[0].runtime.stable_id()
    overlay = AdaptiveOverlay(
        profile_id="coding",
        ordered_primary_runtime_ids=(fallback,),
        reasoning_defaults={},
    )

    with pytest.raises(ValueError, match="exact primary choices"):
        validate_overlay(config, overlay)


def test_overlay_rejects_reasoning_outside_target_bounds(config) -> None:
    from plugins.auto_routing.auto_routing.adaptation import validate_overlay

    profile = config.profiles["coding"]
    primary_id = profile.primary.runtime.stable_id()
    overlay = _overlay(config, defaults={primary_id: "ultra"})

    with pytest.raises(ValueError, match="reasoning effort outside target bounds"):
        validate_overlay(config, overlay)


def test_materialization_reorders_primaries_and_preserves_fallback_bytes(config) -> None:
    from plugins.auto_routing.auto_routing.adaptation import materialize_profile

    profile = config.profiles["coding"]
    challenger_id = profile.primary_challengers[0].runtime.stable_id()
    materialized = materialize_profile(
        profile,
        _overlay(config, defaults={challenger_id: "low"}),
    )

    assert [target.runtime.stable_id() for target in materialized.primary_choices()] == [
        challenger_id,
        profile.primary.runtime.stable_id(),
    ]
    assert materialized.primary.reasoning.default == "low"
    assert materialized.fallbacks == profile.fallbacks
    assert materialized.fallbacks[0].model_dump_json() == profile.fallbacks[0].model_dump_json()


def test_materialized_profile_round_trips_with_active_primary_and_challengers(config) -> None:
    from plugins.auto_routing.auto_routing.adaptation import materialize_profile
    from plugins.auto_routing.auto_routing.models import RouteProfile

    profile = config.profiles["coding"]
    materialized = materialize_profile(profile, _overlay(config))
    reparsed = RouteProfile.model_validate(materialized.model_dump(mode="json"))

    assert reparsed.primary.runtime == profile.primary_challengers[0].runtime
    assert reparsed.primary.revision_status == "active"
    assert [target.revision_status for target in reparsed.primary_challengers] == [
        "challenger"
    ]
    assert reparsed.fallbacks == profile.fallbacks


def test_materialize_profile_rejects_reasoning_outside_target_bounds(config) -> None:
    from plugins.auto_routing.auto_routing.adaptation import materialize_profile

    profile = config.profiles["coding"]
    overlay = _overlay(
        config,
        defaults={profile.primary.runtime.stable_id(): "ultra"},
    )

    with pytest.raises(ValueError, match="reasoning effort outside target bounds"):
        materialize_profile(profile, overlay)


@pytest.mark.parametrize(
    "overlay",
    (
        lambda config: AdaptiveOverlay(
            profile_id="research",
            ordered_primary_runtime_ids=tuple(
                target.runtime.stable_id()
                for target in config.profiles["coding"].primary_choices()
            ),
            reasoning_defaults={},
        ),
        lambda config: AdaptiveOverlay(
            profile_id="coding",
            ordered_primary_runtime_ids=(
                config.profiles["coding"].fallbacks[0].runtime.stable_id(),
            ),
            reasoning_defaults={},
        ),
    ),
)
def test_materialize_profile_rejects_wrong_profile_or_primary_membership(
    config,
    overlay,
) -> None:
    from plugins.auto_routing.auto_routing.adaptation import materialize_profile

    with pytest.raises(ValueError, match="profile|exact primary choices"):
        materialize_profile(config.profiles["coding"], overlay(config))


def test_materialize_profiles_replaces_only_named_profile_overlays(config) -> None:
    from plugins.auto_routing.auto_routing.adaptation import materialize_profiles

    materialized = materialize_profiles(config, {"coding": _overlay(config)})

    assert materialized is not config
    assert materialized.profiles["coding"].primary.runtime == (
        config.profiles["coding"].primary_challengers[0].runtime
    )
    assert materialized.profiles["coding"].primary.revision_status == "active"
    with pytest.raises(TypeError):
        materialized.profiles["new-profile"] = config.profiles["coding"]


def test_static_adaptive_revision_id_is_profile_scoped_and_stable() -> None:
    from plugins.auto_routing.auto_routing.adaptation import static_adaptive_revision_id

    authority_id = "a" * 64

    assert static_adaptive_revision_id(authority_id, "coding") == static_adaptive_revision_id(
        authority_id,
        "coding",
    )
    assert static_adaptive_revision_id(authority_id, "coding") != static_adaptive_revision_id(
        authority_id,
        "research",
    )
