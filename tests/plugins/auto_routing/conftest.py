"""Deterministic shared fixtures for the auto-routing plugin tests."""

from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator

import pytest

from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

from _stage3_test_support import build_stage3_route_harness


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _clear_hermes_path_and_config_caches() -> None:
    """Drop process-local caches that can retain another profile's paths."""
    from agent import skill_utils
    from hermes_cli import config

    with config._CONFIG_LOCK:
        config._LOAD_CONFIG_CACHE.clear()
        config._RAW_CONFIG_CACHE.clear()
        config._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    config.invalidate_env_cache()
    skill_utils._ENV_DETECT_CACHE.clear()
    skill_utils._external_dirs_cache_clear()


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Provide a clean, profile-local ``HERMES_HOME`` for each test."""
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    hermes_home = tmp_path / "profile"
    hermes_home.mkdir()
    override_token = set_hermes_home_override(None)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _clear_hermes_path_and_config_caches()
    try:
        yield hermes_home
    finally:
        _clear_hermes_path_and_config_caches()
        reset_hermes_home_override(override_token)


@dataclass
class MutableClock:
    """Small deterministic clock shared by storage and policy tests."""

    current: datetime

    def now(self) -> datetime:
        return self.current

    def today(self) -> date:
        return self.current.date()

    def advance(self, *, seconds: float) -> datetime:
        self.current += timedelta(seconds=seconds)
        return self.current


@pytest.fixture
def mutable_clock() -> MutableClock:
    return MutableClock(datetime(2026, 1, 1, tzinfo=UTC))


@pytest.fixture
def valid_turn_event():
    """Return one canonical initial routed-turn evidence event."""
    from plugins.auto_routing.auto_routing.evidence import (
        build_context_bucket,
        turn_evidence_id,
    )
    from plugins.auto_routing.auto_routing.models import (
        ComplexityBands,
        EvidenceEvent,
        TaskAssessment,
    )

    assessment = TaskAssessment(
        complexity=0.5,
        domains=("coding",),
        required_capabilities=("tools",),
        required_modalities=("text",),
        expected_context_tokens=4096,
        expected_output_tokens=1024,
        quality_sensitivity=0.8,
        reliability_sensitivity=0.8,
        latency_sensitivity=0.2,
        cost_sensitivity=0.2,
        risk_class="moderate",
        confidence=0.9,
    )
    return EvidenceEvent(
        evidence_id=turn_evidence_id("session-a", "a" * 64),
        source="hermes_turn_outcome",
        signal_type="objective_outcome",
        decision_id="decision-a",
        session_id="session-a",
        turn_id="a" * 64,
        task_id="task-a",
        route_epoch_id="epoch-a",
        runtime_id="b" * 64,
        profile_id="coding",
        reasoning_effort="high",
        context_bucket=build_context_bucket(assessment, ComplexityBands()),
        is_initial_routing_task=True,
        outcome="verified",
        normalized_value=1.0,
        confidence_weight=1.0,
        attribution_confidence=1.0,
        api_calls=1,
        tool_iterations=0,
        retry_count=0,
        cost_usd=0.01,
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=0,
        observed_at="2026-07-17T12:00:00Z",
    )


class RecordingPluginContext(PluginContext):
    """A real ``PluginContext`` with convenient registration snapshots."""

    @property
    def cli_commands(self) -> list[str]:
        return list(self._manager._cli_commands)

    @property
    def skills(self) -> list[str]:
        return list(self._manager._plugin_skills)

    @property
    def tools(self) -> list[str]:
        return sorted(self._manager._plugin_tool_names)

    @property
    def middleware(self) -> list[str]:
        return [
            kind
            for kind, callbacks in self._manager._middleware.items()
            for _callback in callbacks
        ]

    @property
    def hooks(self) -> list[str]:
        return [
            name
            for name, callbacks in self._manager._hooks.items()
            for _callback in callbacks
        ]


@pytest.fixture
def plugin_context(isolated_home: Path) -> RecordingPluginContext:
    del isolated_home
    manifest = PluginManifest(
        name="auto-routing",
        version="0.1.0",
        description=(
            "Profile-local executable-inventory advisor and cache-safe "
            "Auto model router"
        ),
        source="bundled",
        path=PROJECT_ROOT / "plugins" / "auto_routing",
    )
    return RecordingPluginContext(manifest, PluginManager())


@pytest.fixture
def load_bundled_plugin() -> Iterator[Callable[[str], types.ModuleType]]:
    """Load a bundled plugin with the production loader's module identity."""
    loaded_module_names: set[str] = set()

    def load(name: str) -> types.ModuleType:
        plugin_dir = PROJECT_ROOT / "plugins" / name
        init_file = plugin_dir / "__init__.py"
        if not init_file.exists():
            raise FileNotFoundError(f"No __init__.py in {plugin_dir}")

        if "hermes_plugins" not in sys.modules:
            namespace = types.ModuleType("hermes_plugins")
            namespace.__path__ = []  # type: ignore[attr-defined]
            namespace.__package__ = "hermes_plugins"
            sys.modules["hermes_plugins"] = namespace

        slug = name.replace("/", "__").replace("-", "_")
        module_name = f"hermes_plugins.{slug}"
        for loaded_name in tuple(sys.modules):
            if loaded_name == module_name or loaded_name.startswith(f"{module_name}."):
                sys.modules.pop(loaded_name, None)

        spec = importlib.util.spec_from_file_location(
            module_name,
            init_file,
            submodule_search_locations=[str(plugin_dir)],
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot create module spec for {init_file}")

        module = importlib.util.module_from_spec(spec)
        module.__package__ = module_name
        module.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        loaded_module_names.add(module_name)
        return module

    yield load

    for module_name in tuple(sys.modules):
        if any(
            module_name == loaded or module_name.startswith(f"{loaded}.")
            for loaded in loaded_module_names
        ):
            sys.modules.pop(module_name, None)


@pytest.fixture
def service(plugin_context: RecordingPluginContext, load_bundled_plugin):
    module = load_bundled_plugin("auto_routing")
    return module.AutoRoutingService.from_plugin_context(plugin_context)


@pytest.fixture
def active_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    route = build_stage3_route_harness(tmp_path, monkeypatch, execution="primary")
    try:
        yield route
    finally:
        route.close()


@pytest.fixture
def fallback_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    route = build_stage3_route_harness(tmp_path, monkeypatch, execution="fallback")
    try:
        yield route
    finally:
        route.close()


@pytest.fixture
def stage3_profile_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    routes = []

    def build(**kwargs):
        route = build_stage3_route_harness(tmp_path, monkeypatch, **kwargs)
        routes.append(route)
        return route

    try:
        yield build
    finally:
        for route in reversed(routes):
            route.close()


@pytest.fixture
def compressed_route(active_route):
    return active_route.compression_child(
        child_session_id="compressed-child",
        child_task_id="compressed-child-task",
    )
