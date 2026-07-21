"""Signed local ranking packs and current verified-inventory projection."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent.reasoning_support import ReasoningSupport
from plugins.auto_routing.auto_routing.inventory import (
    ExecutableRuntime,
    InventorySnapshot,
    ReasonCodes,
    management_inventory_ineligibility_reasons,
    verified_inventory_candidates,
)
from plugins.auto_routing.auto_routing.models import (
    AccessEconomics,
    RankingPackTrust,
    RuntimeKey,
)
from plugins.auto_routing.auto_routing import ranking_pack as ranking_pack_module
from plugins.auto_routing.auto_routing.ranking_pack import (
    RankingPackError,
    load_verified_ranking_pack,
    ranking_pack_status,
)


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
TEST_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
OTHER_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))


def _public_key_bytes(signer: Ed25519PrivateKey) -> bytes:
    return signer.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _key_id(signer: Ed25519PrivateKey) -> str:
    return hashlib.sha256(_public_key_bytes(signer)).hexdigest()


def _canonical_signed_bytes(document: dict[str, object]) -> bytes:
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    return json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _write_signed_pack(
    home: Path,
    *,
    signer: Ed25519PrivateKey = TEST_PRIVATE_KEY,
    expires_at: datetime | None = None,
    rankings: dict[str, object] | None = None,
    schema_version: int = 1,
    pack_id: str = "pack-2026-07",
    path: Path | None = None,
) -> Path:
    destination = path or home / "auto-routing" / "ranking-packs" / "current.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    document: dict[str, object] = {
        "schema_version": schema_version,
        "pack_id": pack_id,
        "issued_at": (NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "expires_at": (expires_at or NOW + timedelta(days=1))
        .isoformat()
        .replace("+00:00", "Z"),
        "key_id": _key_id(signer),
        "rankings": rankings
        or {
            "a" * 64: {
                "quality": 0.91,
                "reliability": 0.87,
                "latency": 0.62,
                "cost": 0.55,
            }
        },
    }
    try:
        signed_bytes = _canonical_signed_bytes(document)
    except ValueError:
        document["signature"] = base64.b64encode(bytes(64)).decode("ascii")
    else:
        document["signature"] = base64.b64encode(signer.sign(signed_bytes)).decode(
            "ascii"
        )
    destination.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return destination


@pytest.fixture
def trust() -> RankingPackTrust:
    return RankingPackTrust(
        ranking_pack_path="auto-routing/ranking-packs/current.json",
        trusted_ed25519_public_keys=(
            base64.b64encode(_public_key_bytes(TEST_PRIVATE_KEY)).decode("ascii"),
        ),
    )


def test_verified_pack_requires_a_trusted_ed25519_signature(
    tmp_path: Path,
    trust: RankingPackTrust,
) -> None:
    pack_path = _write_signed_pack(tmp_path)

    pack = load_verified_ranking_pack(home=tmp_path, trust=trust, now=NOW)

    assert pack.metadata.pack_id == "pack-2026-07"
    assert pack.metadata.ranking_pack_sha256 == hashlib.sha256(
        pack_path.read_bytes()
    ).hexdigest()
    assert pack.rank_for("a" * 64).quality == pytest.approx(0.91)
    assert pack.rank_for("b" * 64) is None
    assert not hasattr(pack, "signature")
    assert not hasattr(pack, "raw_document")


def test_open_handle_survives_path_replacement_without_reading_replacement(
    tmp_path: Path,
    trust: RankingPackTrust,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted_path = _write_signed_pack(tmp_path, pack_id="trusted-pack")
    replacement_path = _write_signed_pack(
        tmp_path,
        pack_id="replacement-pack",
        path=tmp_path / "replacement.json",
    )
    parked_path = tmp_path / "parked.json"
    original_read = getattr(
        ranking_pack_module,
        "_read_bounded_bytes",
        lambda stream: stream.read(),
    )
    swapped = False

    def swap_then_read(stream):
        nonlocal swapped
        trusted_path.rename(parked_path)
        replacement_path.rename(trusted_path)
        swapped = True
        return original_read(stream)

    monkeypatch.setattr(
        ranking_pack_module,
        "_read_bounded_bytes",
        swap_then_read,
        raising=False,
    )

    pack = load_verified_ranking_pack(home=tmp_path, trust=trust, now=NOW)

    assert swapped is True
    assert pack.metadata.pack_id == "trusted-pack"


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point regression")
def test_windows_intermediate_junction_swap_cannot_replace_trusted_root(
    tmp_path: Path,
    trust: RankingPackTrust,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_signed_pack(tmp_path, pack_id="trusted-pack")
    auto_routing = tmp_path / "auto-routing"
    parked_auto_routing = tmp_path / "parked-auto-routing"
    outside_auto_routing = tmp_path / "outside-auto-routing"
    _write_signed_pack(
        tmp_path,
        pack_id="outside-pack",
        path=outside_auto_routing / "ranking-packs" / "current.json",
    )
    original_validate = ranking_pack_module._windows_validate_handle
    swapped = False

    def validate_then_swap(handle, *, directory, **kwargs):
        nonlocal swapped
        original_validate(handle, directory=directory, **kwargs)
        if (
            directory
            and not swapped
            and ranking_pack_module._windows_final_path(handle)
            .casefold()
            .endswith("\\auto-routing")
        ):
            auto_routing.rename(parked_auto_routing)
            created = subprocess.run(
                [
                    "cmd.exe",
                    "/d",
                    "/c",
                    "mklink",
                    "/J",
                    str(auto_routing),
                    str(outside_auto_routing),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            assert created.returncode == 0, created.stderr or created.stdout
            swapped = True

    monkeypatch.setattr(
        ranking_pack_module,
        "_windows_validate_handle",
        validate_then_swap,
    )

    try:
        with pytest.raises(
            RankingPackError,
            match="ranking_pack_outside_allowed_root",
        ):
            load_verified_ranking_pack(home=tmp_path, trust=trust, now=NOW)
        assert swapped is True
    finally:
        if auto_routing.exists():
            auto_routing.rmdir()
        if parked_auto_routing.exists():
            parked_auto_routing.rename(auto_routing)


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("tampered", "ranking_pack_signature_invalid"),
        ("expired", "ranking_pack_expired"),
        ("unknown_key", "ranking_pack_key_untrusted"),
        ("escape", "ranking_pack_outside_allowed_root"),
    ],
)
def test_invalid_pack_fails_closed_without_inventory_refresh(
    case: str,
    expected_reason: str,
    tmp_path: Path,
    trust: RankingPackTrust,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "plugins.auto_routing.auto_routing.inventory.InventoryService.refresh",
        lambda *_args, **_kwargs: pytest.fail("refresh"),
    )
    if case == "expired":
        _write_signed_pack(tmp_path, expires_at=NOW)
    elif case == "unknown_key":
        _write_signed_pack(tmp_path, signer=OTHER_PRIVATE_KEY)
    elif case == "escape":
        escaped = _write_signed_pack(tmp_path, path=tmp_path / "outside.json")
        trust = trust.model_copy(
            update={"ranking_pack_path": escaped.relative_to(tmp_path).as_posix()}
        )
    else:
        path = _write_signed_pack(tmp_path)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["rankings"]["a" * 64]["quality"] = 0.01
        path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RankingPackError, match=expected_reason) as raised:
        load_verified_ranking_pack(home=tmp_path, trust=trust, now=NOW)

    assert raised.value.reason_code == expected_reason
    assert "pack-2026-07" not in str(raised.value)


@pytest.mark.parametrize(
    "rankings",
    [
        {"short-id": {"quality": 0.5, "reliability": 0.5, "latency": 0.5, "cost": 0.5}},
        {"a" * 64: {"quality": 1.01, "reliability": 0.5, "latency": 0.5, "cost": 0.5}},
        {"a" * 64: {"quality": float("nan"), "reliability": 0.5, "latency": 0.5, "cost": 0.5}},
        {"a" * 64: {"quality": 0.5, "reliability": 0.5, "latency": 0.5}},
    ],
)
def test_pack_rejects_unstable_runtime_ids_and_unbounded_metrics(
    tmp_path: Path,
    trust: RankingPackTrust,
    rankings: dict[str, object],
) -> None:
    _write_signed_pack(tmp_path, rankings=rankings)

    with pytest.raises(RankingPackError, match="ranking_pack_malformed"):
        load_verified_ranking_pack(home=tmp_path, trust=trust, now=NOW)


def test_pack_rejects_unknown_schema_and_status_is_content_free(
    tmp_path: Path,
    trust: RankingPackTrust,
) -> None:
    _write_signed_pack(tmp_path, schema_version=2)

    status = ranking_pack_status(home=tmp_path, trust=trust, now=NOW)

    assert status == {
        "status": "invalid",
        "reason_code": "ranking_pack_malformed",
    }
    assert "current.json" not in json.dumps(status)


@pytest.mark.parametrize("schema_version", [True, "1"])
def test_pack_schema_version_is_the_exact_integer_one(
    tmp_path: Path,
    trust: RankingPackTrust,
    schema_version: object,
) -> None:
    _write_signed_pack(tmp_path, schema_version=schema_version)  # type: ignore[arg-type]

    with pytest.raises(RankingPackError, match="ranking_pack_malformed"):
        load_verified_ranking_pack(home=tmp_path, trust=trust, now=NOW)


def _runtime(
    name: str,
    *,
    local: bool = False,
    state: str = "verified",
    expires_at: datetime | None = None,
    verification_source: str | None = None,
    provenance: tuple[str, ...] | None = None,
    capabilities: dict[str, object] | None = None,
) -> ExecutableRuntime:
    observed = NOW.isoformat().replace("+00:00", "Z")
    if verification_source is None:
        verification_source = "installed_local" if local else "authenticated_live"
    if provenance is None:
        provenance = (
            ("installed-local", "backend-inspection")
            if local
            else ("configured", "authenticated_live")
        )
    return ExecutableRuntime(
        key=RuntimeKey(
            provider="ollama" if local else "openai",
            model=name,
            auth_identity="local:ollama" if local else "api-key:work",
            credential_pool_identity="" if local else "pool:work",
            endpoint_identity="local-backend:ollama" if local else "endpoint:work",
            api_mode="chat_completions",
            local_backend="ollama" if local else "",
            inventory_revision="inventory-current",
        ),
        resolver_name="ollama:default" if local else "openai:work",
        state=state,
        reasons=ReasonCodes(()),
        economics=AccessEconomics(
            billing_kind="local" if local else "metered",
            source_id="current-economics",
            provenance="backend-inspection" if local else "configured-pricing",
            observed_at=observed,
        ),
        reasoning_support=ReasoningSupport(
            efforts=("low", "medium", "high"),
            provider_aliases=(),
            provenance="metadata:reasoning_options",
            exact=True,
        ),
        verification_source=verification_source,
        verified_at=observed,
        verification_expires_at=(expires_at or NOW + timedelta(hours=1))
        .isoformat()
        .replace("+00:00", "Z"),
        provenance=provenance,
        observed_at=observed,
        capabilities=capabilities
        if capabilities is not None
        else {
            "supports_tools": True,
            "hardware_compatible": True,
            "open_weights": True,
            "license_id": "apache-2.0",
        },
    )


def test_candidate_projection_uses_only_current_verified_configured_or_local_runtimes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "plugins.auto_routing.auto_routing.inventory.InventoryService.refresh",
        lambda *_args, **_kwargs: pytest.fail("refresh"),
    )
    remote = _runtime("remote")
    local = _runtime("local", local=True)
    expired = _runtime("expired", expires_at=NOW)
    unverified = _runtime("unverified", state="configured_unverified")
    no_tools = _runtime("no-tools", capabilities={})
    untrusted_source = _runtime(
        "catalog-only",
        verification_source="catalog",
        provenance=("catalog", "static_curated"),
    )
    moa = dataclasses.replace(
        _runtime("moa"),
        key=_runtime("moa").key.model_copy(update={"provider": "moa"}),
    )
    snapshot = InventorySnapshot(
        revision="inventory-current",
        runtimes=[untrusted_source, local, no_tools, expired, remote, moa, unverified],
        observed_at=NOW.isoformat().replace("+00:00", "Z"),
    )

    candidates = verified_inventory_candidates(snapshot, NOW)

    assert tuple(candidate.runtime_id for candidate in candidates) == tuple(
        sorted((remote.key.stable_id(), local.key.stable_id()))
    )
    assert {candidate.key.model for candidate in candidates} == {"remote", "local"}
    assert management_inventory_ineligibility_reasons(expired, NOW) == (
        "runtime_verification_expired",
    )
    assert management_inventory_ineligibility_reasons(unverified, NOW) == (
        "runtime_not_verified",
    )
    assert management_inventory_ineligibility_reasons(no_tools, NOW) == (
        "missing_tools",
    )
    assert management_inventory_ineligibility_reasons(untrusted_source, NOW) == (
        "configured_provider_source_not_allowed",
    )
    assert management_inventory_ineligibility_reasons(moa, NOW) == ("moa_excluded",)


def test_candidate_projection_requires_complete_current_verification_evidence() -> None:
    runtime = dataclasses.replace(_runtime("missing-start"), verified_at=None)
    snapshot = InventorySnapshot(
        revision="inventory-current",
        runtimes=[runtime],
        observed_at=NOW.isoformat().replace("+00:00", "Z"),
    )

    assert verified_inventory_candidates(snapshot, NOW) == ()
    assert management_inventory_ineligibility_reasons(runtime, NOW) == (
        "runtime_not_verified",
    )
