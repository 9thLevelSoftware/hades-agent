"""Profile-home isolation contracts for auto-routing state."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import pytest

from plugins.auto_routing.auto_routing.storage import RoutingStore


@pytest.fixture
def profile_home_factory(tmp_path: Path) -> Callable[[str], Path]:
    def create(name: str) -> Path:
        home = tmp_path / "profiles" / name
        home.mkdir(parents=True)
        return home

    return create


def test_profiles_use_distinct_state_databases(
    profile_home_factory: Callable[[str], Path],
) -> None:
    default_home = profile_home_factory("default")
    work_home = profile_home_factory("work")
    default_store = RoutingStore.open(home=default_home)
    work_store = RoutingStore.open(home=work_home)
    try:
        default_store.write_authority_revision(
            "auth-default",
            {"profiles": {}},
            created_at="2026-01-01T00:00:00Z",
        )

        assert default_store.path == default_home / "auto-routing" / "state.db"
        assert work_store.path == work_home / "auto-routing" / "state.db"
        assert work_store.read_authority_revision("auth-default") is None
    finally:
        default_store.close()
        work_store.close()


def test_adaptation_controls_and_history_remain_profile_home_local(
    profile_home_factory: Callable[[str], Path],
) -> None:
    first_store = RoutingStore.open(home=profile_home_factory("adaptive-first"))
    second_store = RoutingStore.open(home=profile_home_factory("adaptive-second"))
    try:
        first = first_store.set_profile_freeze(
            "a" * 64,
            "coding",
            frozen=True,
            expected_generation=0,
        )

        assert first.frozen is True
        assert second_store.read_profile_control("a" * 64, "coding").frozen is False
        assert len(first_store.list_adaptive_lifecycle_events("a" * 64, "coding")) == 1
        assert second_store.list_adaptive_lifecycle_events("a" * 64, "coding") == ()
    finally:
        first_store.close()
        second_store.close()


def test_environment_selected_profiles_never_share_rows(
    profile_home_factory: Callable[[str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_home = profile_home_factory("first")
    second_home = profile_home_factory("second")

    monkeypatch.setenv("HERMES_HOME", str(first_home))
    first = RoutingStore.open()
    try:
        first.write_authority_revision(
            "first-only",
            {"profiles": {"first": {}}},
            created_at="2026-01-01T00:00:00Z",
        )
    finally:
        first.close()

    monkeypatch.setenv("HERMES_HOME", str(second_home))
    second = RoutingStore.open()
    try:
        assert second.path == second_home / "auto-routing" / "state.db"
        assert second.read_authority_revision("first-only") is None
        second.write_authority_revision(
            "second-only",
            {"profiles": {"second": {}}},
            created_at="2026-01-01T00:00:00Z",
        )
    finally:
        second.close()

    first_again = RoutingStore.open(home=first_home)
    try:
        assert first_again.read_authority_revision("first-only") is not None
        assert first_again.read_authority_revision("second-only") is None
    finally:
        first_again.close()


def test_same_session_and_turn_ids_remain_profile_local(stage3_profile_factory):
    shared = {
        "session_id": "same-session",
        "task_id": "same-task",
        "turn_id": "same-turn",
    }
    profile_a = stage3_profile_factory(profile_name="a", **shared)
    profile_b = stage3_profile_factory(profile_name="b", **shared)

    with profile_a.activate_profile():
        event_a = profile_a.service.ingest_turn_outcome(profile_a.payload())
    with profile_b.activate_profile():
        event_b = profile_b.service.ingest_turn_outcome(profile_b.payload())

    assert event_a.event.evidence_id == event_b.event.evidence_id
    assert profile_a.service.store.count_evidence_events() == 1
    assert profile_b.service.store.count_evidence_events() == 1
    assert profile_a.service.store.path != profile_b.service.store.path


def test_dead_thread_evidence_services_are_retired_from_profile_cache(active_route):
    from plugins.auto_routing.auto_routing.runtime_resolver import (
        AutoRoutingRuntimeResolver,
    )

    created = []

    def factory():
        service = active_route.fresh_service(allow_cross_thread_close=True)
        created.append(service)
        return service

    resolver = AutoRoutingRuntimeResolver(
        plugin_context=object(),
        home_resolver=lambda: active_route.home,
        service_factory=factory,
    )

    def acquire(_index):
        return resolver.service_for_current_profile()

    try:
        for index in range(8):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(acquire, index).result()
        main_service = resolver.service_for_current_profile()

        assert len(resolver._services) == 1
        assert len(created) == 9
        assert sum(service is main_service for service in created) == 1
        for service in created[:-1]:
            with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
                service.store.count_evidence_events()
    finally:
        resolver.close()


def test_future_schema_observer_does_not_fall_back_to_cached_profile(
    active_route,
    tmp_path: Path,
):
    from plugins.auto_routing.auto_routing.runtime_resolver import (
        AutoRoutingRuntimeResolver,
    )
    from plugins.auto_routing.auto_routing.service import AutoRoutingService

    future_home = tmp_path / "future-profile"
    future_db = future_home / "auto-routing" / "state.db"
    future_db.parent.mkdir(parents=True)
    connection = sqlite3.connect(future_db)
    try:
        connection.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', '999')"
        )
        connection.commit()
    finally:
        connection.close()

    current_home = [active_route.home]
    services = []

    def factory():
        home = current_home[0]
        service = AutoRoutingService(
            plugin_context=active_route.service.plugin_context,
            hermes_home=home,
            store=RoutingStore.open(
                home=home,
                allow_cross_thread_close=True,
            ),
            adapter=active_route._adapter,
            _pinned_config_path=home / "config.yaml",
        )
        services.append(service)
        return service

    resolver = AutoRoutingRuntimeResolver(
        plugin_context=active_route.service.plugin_context,
        home_resolver=lambda: current_home[0],
        service_factory=factory,
    )
    try:
        resolver.service_for_current_profile()
        assert active_route.service.store.count_evidence_events() == 0
        current_home[0] = future_home

        assert resolver.on_post_turn_outcome(**active_route.payload()) is None

        assert active_route.service.store.count_evidence_events() == 0
        assert len(services) == 1
    finally:
        resolver.close()
