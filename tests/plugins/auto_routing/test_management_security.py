"""Adversarial side-effect boundaries for Stage 5 profile management."""

from __future__ import annotations

import base64
import socket
import urllib.request
from datetime import timedelta

import httpx
import pytest
import requests

from agent import moa_loop
from plugins.auto_routing.auto_routing.catalog import CatalogService
from plugins.auto_routing.auto_routing.inventory import InventoryService
from plugins.auto_routing.auto_routing.models import RankingPackTrust
from plugins.auto_routing.auto_routing.service import AutoRoutingService
from test_management_reconciler import NOW, NOW_TEXT, _config, _observation, _service
from test_ranking_pack import (
    TEST_PRIVATE_KEY,
    _public_key_bytes,
    _write_signed_pack,
)


def _forbidden(label: str):
    def fail(*_args, **_kwargs):
        raise AssertionError(f"{label} must not run during management reconciliation")

    return fail


def _trusted_management_config():
    config = _config()
    trust = RankingPackTrust(
        ranking_pack_path="auto-routing/ranking-packs/current.json",
        trusted_ed25519_public_keys=(
            base64.b64encode(_public_key_bytes(TEST_PRIVATE_KEY)).decode("ascii"),
        ),
    )
    settings = config.autonomous_profile_management.model_copy(
        update={"ranking_pack": trust}
    )
    return config.model_copy(
        update={"autonomous_profile_management": settings}
    )


def test_reconciliation_never_calls_network_probe_catalog_or_moa(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _trusted_management_config()
    service = _service(tmp_path, config)
    try:
        observations = (_observation("challenger"), _observation("current"))
        service.store.write_inventory_snapshot(
            "inventory-current",
            observations,
            created_at=NOW_TEXT,
        )
        _write_signed_pack(
            tmp_path,
            expires_at=NOW + timedelta(days=1),
            rankings={
                observations[0].key.stable_id(): {
                    "quality": 0.95,
                    "reliability": 0.95,
                    "latency": 0.10,
                    "cost": 0.10,
                },
                observations[1].key.stable_id(): {
                    "quality": 0.70,
                    "reliability": 0.70,
                    "latency": 0.30,
                    "cost": 0.30,
                },
            },
        )

        forbidden_paths = (
            (InventoryService, "refresh", "inventory refresh"),
            (InventoryService, "apply_verification", "paid access verification"),
            (CatalogService, "refresh", "catalog refresh"),
            (AutoRoutingService, "verify_runtime", "runtime verification"),
        )
        for owner, method, label in forbidden_paths:
            monkeypatch.setattr(owner, method, _forbidden(label))
        network_forbidden = _forbidden("network access")
        monkeypatch.setattr(socket, "create_connection", network_forbidden)
        monkeypatch.setattr(urllib.request, "urlopen", network_forbidden)
        monkeypatch.setattr(requests, "get", network_forbidden)
        monkeypatch.setattr(requests.Session, "request", network_forbidden)
        monkeypatch.setattr(httpx.Client, "request", network_forbidden)
        monkeypatch.setattr(
            moa_loop,
            "aggregate_moa_context",
            _forbidden("MoA evaluation"),
        )

        report = service.reconcile_management(now=NOW)

        assert report.changed is True
        assert report.reason_code == "revision_applied"
    finally:
        service.close()
