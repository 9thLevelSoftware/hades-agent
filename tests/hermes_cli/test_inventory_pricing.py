"""Tests for inventory._apply_pricing — the pricing/tier enrichment that

feeds the desktop GUI model picker (and onboarding) so it can show $/Mtok
columns + Free/Pro badges and gate paid models on free Nous accounts, the
same way the `hermes model` CLI picker does.
"""

import json
from datetime import UTC, datetime, timedelta

import hades_cli.inventory as inv
import hades_cli.models as models_mod


def _patch_pricing(monkeypatch, *, free_tier, pricing, unavailable=None):
    monkeypatch.setattr(models_mod, "get_pricing_for_provider", lambda slug, **kw: pricing.get(slug, {}))
    monkeypatch.setattr(models_mod, "check_nous_free_tier", lambda *, force_fresh=False: free_tier)
    monkeypatch.setattr(
        models_mod, "partition_nous_models_by_tier",
        lambda ids, pr, free_tier: (
            [m for m in ids if m not in (unavailable or [])],
            list(unavailable or []),
        ),
    )


def test_apply_pricing_formats_per_model_prices(monkeypatch):
    """Each model gets formatted input/output/cache + a free flag."""
    _patch_pricing(
        monkeypatch,
        free_tier=False,
        pricing={
            "openrouter": {
                "a/paid": {"prompt": "0.000003", "completion": "0.000015", "input_cache_read": "0.0000003"},
                "b/free": {"prompt": "0", "completion": "0"},
            }
        },
    )
    rows = [{"slug": "openrouter", "models": ["a/paid", "b/free"]}]
    inv._apply_pricing(rows)

    pricing = rows[0]["pricing"]
    assert pricing["a/paid"] == {"input": "$3.00", "output": "$15.00", "cache": "$0.30", "free": False}
    assert pricing["b/free"]["free"] is True
    assert pricing["b/free"]["input"] == "free"


def test_apply_pricing_nous_free_tier_gates_paid_models(monkeypatch):
    """A free-tier Nous account marks paid models unavailable and sets the flag."""
    _patch_pricing(
        monkeypatch,
        free_tier=True,
        pricing={
            "nous": {
                "free/model": {"prompt": "0", "completion": "0"},
                "paid/model": {"prompt": "0.000005", "completion": "0.00001"},
            }
        },
        unavailable=["paid/model"],
    )
    rows = [{"slug": "nous", "models": ["free/model", "paid/model"]}]
    inv._apply_pricing(rows)

    assert rows[0]["free_tier"] is True
    assert rows[0]["unavailable_models"] == ["paid/model"]
    assert rows[0]["pricing"]["free/model"]["free"] is True


def test_apply_pricing_nous_paid_tier_no_gating(monkeypatch):
    """A paid Nous account gates nothing."""
    _patch_pricing(
        monkeypatch,
        free_tier=False,
        pricing={"nous": {"x/model": {"prompt": "0.000001", "completion": "0.000002"}}},
    )
    rows = [{"slug": "nous", "models": ["x/model"]}]
    inv._apply_pricing(rows)

    assert rows[0]["free_tier"] is False
    assert rows[0]["unavailable_models"] == []


def test_apply_pricing_skips_providers_without_pricing(monkeypatch):
    """A provider with no live pricing simply gets no pricing key."""
    _patch_pricing(monkeypatch, free_tier=False, pricing={})
    rows = [{"slug": "anthropic", "models": ["claude-x"]}]
    inv._apply_pricing(rows)

    assert "pricing" not in rows[0]


def test_apply_pricing_failure_is_swallowed(monkeypatch):
    """A pricing fetch that raises must not break the whole payload."""
    def boom(slug, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(models_mod, "get_pricing_for_provider", boom)
    rows = [{"slug": "openrouter", "models": ["a/b"]}]
    inv._apply_pricing(rows)  # must not raise

    assert "pricing" not in rows[0]


def test_opt_in_pricing_evidence_keeps_raw_precision_and_cache_timestamp(
    monkeypatch,
) -> None:
    models_mod._pricing_cache.clear()
    models_mod._pricing_snapshot_cache.clear()
    current = [datetime(2026, 1, 1, tzinfo=UTC)]
    physical_calls: list[str] = []
    payload = {
        "data": [
            {
                "id": "vendor/tiny",
                "pricing": {
                    "prompt": "0.0000000001",
                    "completion": "0.0000000002",
                },
            }
        ]
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(payload).encode()

    def open_catalog(request, *, timeout):
        del timeout
        physical_calls.append(request.full_url)
        return Response()

    monkeypatch.setattr(models_mod, "_pricing_now", lambda: current[0], raising=False)
    monkeypatch.setattr(models_mod, "_urlopen_model_catalog_request", open_catalog)
    monkeypatch.setattr(models_mod, "_resolve_openrouter_api_key", lambda: "")

    first_rows = [
        {
            "slug": "openrouter",
            "models": ["vendor/tiny"],
            "discovery": {},
        }
    ]
    inv._apply_pricing(first_rows)

    assert "pricing" in first_rows[0]["discovery"]
    first_evidence = first_rows[0]["discovery"]["pricing"]["vendor/tiny"]
    assert first_evidence["input_usd_per_token"] == "0.0000000001"
    assert first_evidence["output_usd_per_token"] == "0.0000000002"
    assert first_evidence["observed_at"] == "2026-01-01T00:00:00Z"

    current[0] += timedelta(minutes=10)
    second_rows = [
        {
            "slug": "openrouter",
            "models": ["vendor/tiny"],
            "discovery": {},
        }
    ]
    inv._apply_pricing(second_rows)

    second_evidence = second_rows[0]["discovery"]["pricing"]["vendor/tiny"]
    assert second_evidence["observed_at"] == first_evidence["observed_at"]
    assert len(physical_calls) == 1


def test_expired_pricing_refresh_failure_keeps_stale_display_without_restamping(
    monkeypatch,
) -> None:
    models_mod._pricing_cache.clear()
    models_mod._pricing_snapshot_cache.clear()
    current = [datetime(2026, 1, 1, tzinfo=UTC)]
    fail_refresh = [False]
    physical_calls = [0]
    payload = {
        "data": [
            {
                "id": "vendor/model",
                "pricing": {
                    "prompt": "0.000001",
                    "completion": "0.000002",
                },
            }
        ]
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(payload).encode()

    def open_catalog(_request, *, timeout):
        del timeout
        physical_calls[0] += 1
        if fail_refresh[0]:
            raise OSError("pricing endpoint unavailable")
        return Response()

    monkeypatch.setattr(models_mod, "_pricing_now", lambda: current[0], raising=False)
    monkeypatch.setattr(models_mod, "_urlopen_model_catalog_request", open_catalog)
    monkeypatch.setattr(models_mod, "_resolve_openrouter_api_key", lambda: "")

    first_rows = [
        {
            "slug": "openrouter",
            "models": ["vendor/model"],
            "discovery": {},
        }
    ]
    inv._apply_pricing(first_rows)
    first_observed_at = first_rows[0]["discovery"]["pricing"][
        "vendor/model"
    ]["observed_at"]

    current[0] += timedelta(days=2)
    fail_refresh[0] = True
    stale_rows = [
        {
            "slug": "openrouter",
            "models": ["vendor/model"],
            "discovery": {},
        }
    ]
    inv._apply_pricing(stale_rows)

    assert stale_rows[0]["pricing"]["vendor/model"]["input"] == "$1.00"
    stale_evidence = stale_rows[0]["discovery"]["pricing"]["vendor/model"]
    assert stale_evidence["observed_at"] == first_observed_at
    assert stale_evidence["fresh"] is False
    assert physical_calls[0] == 2
