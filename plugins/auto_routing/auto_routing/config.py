"""Pure parsing and authority hashing for auto-routing configuration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

from .models import (
    AutoRoutingConfig,
    ProfileAdaptationSettings,
    RouteProfile,
    RoutingTarget,
    RuntimeKey,
)


_DEFAULT_PROFILE_ADAPTATION = ProfileAdaptationSettings()


class ConfigError(ValueError):
    """Raised when the auto-routing authority subtree is absent or invalid."""


def parse_config(root: Mapping[str, Any]) -> AutoRoutingConfig:
    """Parse only ``plugins.entries.auto-routing`` from a read-only root."""
    try:
        plugins = root["plugins"]
        entries = plugins["entries"]
        raw_config = entries["auto-routing"]
    except (KeyError, TypeError) as exc:
        raise ConfigError(
            "missing required plugins.entries.auto-routing mapping"
        ) from exc

    if not isinstance(raw_config, Mapping):
        raise ConfigError("plugins.entries.auto-routing must be a mapping")

    try:
        return AutoRoutingConfig.model_validate(raw_config)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from None


def authority_revision(config: AutoRoutingConfig) -> str:
    """Hash all user authority while excluding inventory/catalog observations."""
    payload = json.dumps(
        authority_document(config),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def authority_document(config: AutoRoutingConfig) -> dict[str, Any]:
    """Return canonical user authority without observation-only metadata."""
    value = _authority_value(config)
    if not isinstance(value, dict):  # pragma: no cover - model invariant
        raise TypeError("auto-routing authority must serialize as a mapping")
    return value


def config_document(config: AutoRoutingConfig) -> dict[str, Any]:
    """Serialize persisted authority with explicit profile-local intent.

    Optional metadata remains compact, while ``RouteProfile.limits`` is always
    materialized and per-profile adaptation defaults are canonicalized. This
    preserves the authoring distinction between an omitted answer and the
    canonical, explicit ``null`` inheritance choice without allowing the
    legacy top-level adaptation block to opt profiles in.
    """
    document = config.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
        warnings=False,
    )
    for profile_id, profile in config.profiles.items():
        profile_document = document["profiles"][profile_id]
        profile_document["limits"] = (
            None
            if profile.limits is None
            else profile.limits.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=False,
            )
        )
        if not profile.primary_challengers:
            profile_document.pop("primary_challengers", None)
        if _has_profile_adaptation_authority(profile):
            profile_document["adaptation"] = profile.adaptation.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=False,
            )
        else:
            profile_document.pop("adaptation", None)
    document["autonomous_profile_management"] = (
        config.autonomous_profile_management.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
        )
    )
    return document


def management_authority_revision(config: AutoRoutingConfig) -> str:
    """Hash only the canonical autonomous profile-management authority."""
    payload = json.dumps(
        config.autonomous_profile_management.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def config_revision(config: AutoRoutingConfig) -> str:
    """Hash the complete normalized plugin config used by activation receipts."""
    payload = json.dumps(
        config_document(config),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _authority_value(value: Any) -> Any:
    if isinstance(value, RuntimeKey):
        return {
            name: _authority_value(getattr(value, name))
            for name in type(value).model_fields
            if name != "inventory_revision"
        }
    if isinstance(value, RoutingTarget):
        return {
            name: _authority_value(getattr(value, name))
            for name in type(value).model_fields
            if name != "supported_reasoning_efforts"
        }
    if isinstance(value, RouteProfile):
        document = {}
        for name in type(value).model_fields:
            if name == "provenance":
                continue
            if name == "primary_challengers" and not value.primary_challengers:
                continue
            if name == "adaptation" and not _has_profile_adaptation_authority(value):
                continue
            document[name] = _authority_value(getattr(value, name))
        return document
    if isinstance(value, BaseModel):
        return {
            name: _authority_value(getattr(value, name))
            for name in type(value).model_fields
        }
    if isinstance(value, Mapping):
        return {str(key): _authority_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_authority_value(item) for item in value]
    return value


def _has_profile_adaptation_authority(profile: RouteProfile) -> bool:
    """Keep only nondefault staged or enabled profile-local policy in hashes."""
    return profile.adaptation != _DEFAULT_PROFILE_ADAPTATION


def _format_validation_error(error: ValidationError) -> str:
    messages: list[str] = []
    for detail in error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    ):
        location = ".".join(str(part) for part in detail["loc"])
        prefix = f"{location}: " if location else ""
        messages.append(f"{prefix}{detail['msg']}")
    return "; ".join(messages)


__all__ = [
    "ConfigError",
    "authority_document",
    "authority_revision",
    "config_document",
    "config_revision",
    "management_authority_revision",
    "parse_config",
]
