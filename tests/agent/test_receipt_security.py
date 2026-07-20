"""Tests for receipt redaction, export, retention, and gated signing (Task 7).

Covers `agent/receipt_security.py` against a real profile-local
``SessionDB``, real files, and real hashes:

- Redaction removes credential-like keys, bearer tokens, URL
  userinfo/query secrets, undeclared message bodies, and home/profile
  path prefixes before content is hashed or exported.
- Public export contains canonical receipt/observations/attestations
  plus hash-verification data and never a raw local locator; local
  export may include profile-relative locators only.
- Bundles name artifacts from ``artifact_id`` + sanitized extension,
  re-hash bytes while copying, and fail on mismatch; a symlinked output
  path is refused.
- Signing is service-gated: no provider loads until config names it and
  its ``check_fn`` passes. A valid signature proves provenance over a
  content hash and never promotes ``completed_unverified``.
- Retention is explicit: ``plan()`` returns exact IDs and blockers,
  ``prune()`` revalidates the plan, refuses holds, deletes expired raw
  artifact locators before receipt rows, appends immutable deletion
  tombstones, and is replay-safe.
"""

from __future__ import annotations

import base64
import hashlib
import hmac as hmac_module
import json
import logging
import os
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from hades_state import SessionDB
from agent.receipt_artifacts import ArtifactCatalog
from agent.receipt_hashing import canonical_content_hash, hash_hex
from agent.receipt_models import (
    build_claim,
    build_evidence_digest,
    build_observation,
    build_receipt,
    build_requested_outcome,
)
from agent.receipt_store import ReceiptAttestation, ReceiptStore
from agent.receipt_security import (
    ReceiptExportError,
    ReceiptExporter,
    ReceiptRedactor,
    ReceiptRetentionService,
    ReceiptSigningService,
    RetentionHold,
    RetentionHoldError,
    RetentionPlanMismatch,
    SignatureMaterial,
    SigningUnavailableError,
    register_receipt_signer,
    unregister_receipt_signer,
    verify_export_hashes,
)
from agent.receipts import ReceiptSourceKey

NOW = "2026-07-19T12:00:00Z"
OLD_DECIDED_AT = "2024-01-01T00:00:00Z"
RECENT_DECIDED_AT = "2026-07-10T00:00:00Z"

_HMAC_KEY = b"test-signing-key"


# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture()
def home(tmp_path, monkeypatch):
    profile_home = tmp_path / "profile-home"
    profile_home.mkdir()
    monkeypatch.setenv("HADES_HOME", str(profile_home))
    return profile_home


@pytest.fixture()
def db(home):
    session_db = SessionDB(db_path=home / "state.db")
    yield session_db
    session_db.close()


@pytest.fixture()
def store(db):
    return ReceiptStore(db)


@pytest.fixture()
def catalog(db):
    return ArtifactCatalog(db)


def _make_receipt(
    *,
    source_id: str = "s1:t1",
    session_id: str | None = "s1",
    turn_id: str | None = "t1",
    source_kind: str = "turn",
    subject_kind: str = "turn",
    mission_id: str | None = None,
    transaction_id: str | None = None,
    decided_at: str = RECENT_DECIDED_AT,
    statement: str = "README contains marker",
    artifacts=(),
):
    evidence = build_evidence_digest(
        evidence_kind="verification_check",
        source_ref=f"verification_evidence.db:check:{source_id}",
        producer_id="hades.verification",
        observed_at=decided_at,
        summary="pytest passed after final edit",
        payload_hash=canonical_content_hash({"check": "pytest", "result": "pass"}),
    )
    claim = build_claim(
        statement=statement,
        evidence_ids=(evidence.evidence_id,),
        artifact_ids=tuple(a.artifact_id for a in artifacts),
        verdict="satisfied",
    )
    outcome = build_requested_outcome(
        outcome_kind="code_change",
        description="add marker to README",
        producer_id="hades.turn-ledger",
    )
    return build_receipt(
        source=ReceiptSourceKey(source_kind, source_id),
        subject_kind=subject_kind,
        subject_id=source_id,
        session_id=session_id,
        turn_id=turn_id,
        mission_id=mission_id,
        transaction_id=transaction_id,
        requested_outcome=outcome,
        status="completed_unverified",
        claims=(claim,),
        evidence=(evidence,),
        artifacts=tuple(artifacts),
        scorer_id="hades.receipts.default",
        scorer_version="1.0",
        decided_at=decided_at,
    )


@pytest.fixture()
def completed_receipt(store):
    receipt = _make_receipt()
    return store.insert(receipt)


class _HmacSigner:
    """Deterministic local test signer (external boundary stand-in)."""

    provider_id = "test-hmac"

    def sign(self, content_hash: str) -> SignatureMaterial:
        digest = hmac_module.new(
            _HMAC_KEY, content_hash.encode("utf-8"), hashlib.sha256
        ).digest()
        return SignatureMaterial(
            key_id="k1",
            algorithm="hmac-sha256",
            signature_b64=base64.b64encode(digest).decode("ascii"),
        )

    def verify(self, content_hash: str, material: SignatureMaterial) -> bool:
        expected = self.sign(content_hash).signature_b64
        return hmac_module.compare_digest(expected, material.signature_b64)


class _CountingFactory:
    def __init__(self):
        self.calls = 0

    def __call__(self, config: dict):
        self.calls += 1
        return _HmacSigner()


@pytest.fixture()
def signer_factory():
    factory = _CountingFactory()
    register_receipt_signer("test-hmac", factory, lambda config: True)
    yield factory
    unregister_receipt_signer("test-hmac")


@pytest.fixture()
def config():
    return {
        "receipts": {
            "mode": "capture",
            "retention_days": 365,
            "artifact_locator_retention_days": 90,
            "export_redaction": "public",
            "signing": {"provider": "", "required": False},
        }
    }


@pytest.fixture()
def signing_service(store, signer_factory, config):
    config["receipts"]["signing"] = {"provider": "test-hmac", "required": False}
    return ReceiptSigningService.from_config(config, store=store)


class _SecurityHarness:
    def __init__(self, home, store, exporter, receipt, out_dir):
        self.home = home
        self.store = store
        self.exporter = exporter
        self.receipt = receipt
        self.out_dir = out_dir

    def export_public(self) -> Path:
        return self.exporter.export(
            self.receipt.receipt_id,
            self.out_dir / "receipt.json",
            redaction="public",
        )

    def export_local(self) -> Path:
        return self.exporter.export(
            self.receipt.receipt_id,
            self.out_dir / "receipt-local.json",
            redaction="local",
        )


@pytest.fixture()
def security_harness(tmp_path, home, store, catalog):
    artifact_dir = home / "artifacts"
    artifact_dir.mkdir()
    artifact_path = artifact_dir / "proof.txt"
    artifact_path.write_bytes(b"artifact proof bytes")
    digest = catalog.register_path(
        artifact_path,
        source_kind="execute_code",
        source_ref="s1:t1:call-1",
        allowed_roots=(home,),
    )
    redactor = ReceiptRedactor()
    # The producer pipeline redacts BEFORE the canonical content is
    # hashed or persisted — the stored receipt itself is clean.
    raw_payload = {
        "authorization": "Bearer sk-live-secret",
        "callback": (
            "https://ops:sk-live-secret@example.com/hook"
            "?api_key=sk-live-secret"
        ),
        "note": f"saved under {home}/artifacts/proof.txt",
    }
    observed_json = json.dumps(
        redactor.redact(raw_payload), sort_keys=True, separators=(",", ":")
    )
    evidence = build_evidence_digest(
        evidence_kind="verification_check",
        source_ref="verification_evidence.db:check:harness",
        producer_id="hades.verification",
        observed_at=RECENT_DECIDED_AT,
        summary="delivery check passed",
        payload_hash=canonical_content_hash({"check": "delivery"}),
    )
    claim = build_claim(
        statement="the report artifact was produced",
        observed_json=observed_json,
        evidence_ids=(evidence.evidence_id,),
        artifact_ids=(digest.artifact_id,),
        verdict="satisfied",
    )
    outcome = build_requested_outcome(
        outcome_kind="code_change",
        description="produce the report artifact",
        producer_id="hades.turn-ledger",
    )
    receipt = build_receipt(
        source=ReceiptSourceKey("turn", "s1:t9"),
        subject_kind="turn",
        subject_id="s1:t9",
        session_id="s1",
        turn_id="t9",
        requested_outcome=outcome,
        status="completed_unverified",
        claims=(claim,),
        evidence=(evidence,),
        artifacts=(digest,),
        scorer_id="hades.receipts.default",
        scorer_version="1.0",
        decided_at=RECENT_DECIDED_AT,
    )
    store.insert(receipt)
    out_dir = tmp_path / "exports"
    out_dir.mkdir()
    exporter = ReceiptExporter(store, allowed_roots=(home,))
    return _SecurityHarness(home, store, exporter, receipt, out_dir)


# =========================================================================
# Redaction
# =========================================================================


def test_redactor_masks_credential_like_keys():
    redactor = ReceiptRedactor()
    out = redactor.redact(
        {
            "api_key": "sk-live-secret",
            "nested": {"Authorization": "Bearer abc.def", "password": "hunter2"},
            "safe": "plain value",
        }
    )
    text = json.dumps(out)
    assert "sk-live-secret" not in text
    assert "hunter2" not in text
    assert "abc.def" not in text
    assert out["safe"] == "plain value"


def test_redactor_masks_bearer_and_url_secrets():
    redactor = ReceiptRedactor()
    text = redactor.redact_text(
        "call with Bearer sk-live-secret then GET "
        "https://ops:sk-live-secret@h.example/x?token=sk-live-secret&y=2"
    )
    assert "sk-live-secret" not in text
    assert "h.example" in text


def test_redactor_masks_home_and_sensitive_roots(home, tmp_path):
    sensitive = tmp_path / "vault"
    redactor = ReceiptRedactor(sensitive_roots=(sensitive,))
    text = redactor.redact_text(
        f"wrote {home}/artifacts/a.txt and {sensitive}/keys.pem"
    )
    assert str(home) not in text
    assert str(sensitive) not in text


def test_redactor_masks_message_bodies_unless_declared_evidence():
    redactor = ReceiptRedactor()
    out = redactor.redact({"body": "private message text", "subject": "hello"})
    assert out["body"] != "private message text"
    assert "private message text" not in json.dumps(out)
    assert out["subject"] == "hello"
    declared = ReceiptRedactor(allowed_evidence_keys=frozenset({"body"}))
    assert declared.redact({"body": "private message text"})["body"] == (
        "private message text"
    )


# =========================================================================
# Export
# =========================================================================


def test_public_export_contains_no_secret_or_raw_locator(security_harness):
    exported = security_harness.export_public()
    text = exported.read_text("utf-8")
    assert "sk-live-secret" not in text
    assert str(security_harness.home) not in text
    assert "artifact_locations" not in text
    assert verify_export_hashes(exported)


def test_export_verification_detects_tampering(security_harness):
    exported = security_harness.export_public()
    data = json.loads(exported.read_text("utf-8"))
    data["receipt"]["claims"][0]["statement"] = "a different claim"
    exported.write_text(json.dumps(data), encoding="utf-8")
    assert not verify_export_hashes(exported)


def test_export_with_observation_chain_and_attestation_validates_every_hash(
    security_harness, signing_service
):
    """Task 11: the full export (receipt + observation chain + signed
    attestation) independently validates every canonical content hash,
    and tampering with any observation byte is detected."""
    receipt = security_harness.receipt
    observation = build_observation(
        receipt_id=receipt.receipt_id,
        previous_observation_id=None,
        status="failed",
        evidence=receipt.evidence,
        uncertainty=("artifact hash changed on recheck",),
        scorer_id="hades.receipts.default",
        scorer_version="1.0",
        observed_at=RECENT_DECIDED_AT,
    )
    security_harness.store.append_observation(observation)
    attestation = signing_service.sign(receipt)
    assert attestation is not None

    exported = security_harness.exporter.export(
        receipt.receipt_id,
        security_harness.out_dir / "chain.json",
        redaction="public",
    )
    data = json.loads(exported.read_text("utf-8"))
    assert [o["observation_id"] for o in data["observations"]] == [
        observation.observation_id
    ]
    assert [a["attestation_id"] for a in data["attestations"]] == [
        attestation.attestation_id
    ]
    assert verify_export_hashes(exported)
    # The signature travels as provenance and never changes the exported
    # or stored truth status.
    assert data["receipt"]["status"] == "completed_unverified"
    assert (
        security_harness.store.get(receipt.receipt_id).status
        == "completed_unverified"
    )

    # Tampering with an observation byte fails independent validation.
    data["observations"][0]["status"] = "verified"
    exported.write_text(json.dumps(data), encoding="utf-8")
    assert not verify_export_hashes(exported)


def test_local_export_includes_profile_relative_locators_only(security_harness):
    exported = security_harness.export_local()
    text = exported.read_text("utf-8")
    assert str(security_harness.home) not in text
    data = json.loads(text)
    locators = data["profile_relative_locators"]
    assert locators, "local export should carry profile-relative locators"
    for entry in locators:
        path = entry["path"]
        assert not Path(path).is_absolute()
        assert ".." not in Path(path).parts
    assert verify_export_hashes(exported)


def test_export_refuses_symlink_output_path(security_harness, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("do not clobber", encoding="utf-8")
    link = security_harness.out_dir / "receipt-link.json"
    try:
        os.symlink(victim, link)
    except (OSError, NotImplementedError):
        pytest.skip("platform does not permit creating symlinks")
    with pytest.raises(ReceiptExportError):
        security_harness.exporter.export(
            security_harness.receipt.receipt_id, link, redaction="public"
        )
    assert victim.read_text(encoding="utf-8") == "do not clobber"


def test_bundle_names_come_from_artifact_id_not_display_name(
    security_harness,
):
    receipt = security_harness.receipt
    artifact = receipt.artifacts[0]
    bundle = security_harness.exporter.export(
        receipt.receipt_id,
        security_harness.out_dir / "receipt-bundle.zip",
        redaction="public",
        bundle_artifacts=True,
    )
    with zipfile.ZipFile(bundle) as archive:
        names = archive.namelist()
        assert "receipt.json" in names
        artifact_names = [n for n in names if n.startswith("artifacts/")]
        assert artifact_names == [f"artifacts/{artifact.artifact_id}.txt"]
        for name in names:
            assert ".." not in name
            assert not Path(name).is_absolute()
        payload = archive.read(f"artifacts/{artifact.artifact_id}.txt")
        assert hashlib.sha256(payload).hexdigest() == artifact.sha256


def test_bundle_rehashes_and_fails_on_mismatch(security_harness, home):
    # Swap the artifact bytes after registration: the copy re-hash must
    # detect the mismatch and fail the bundle instead of shipping it.
    (home / "artifacts" / "proof.txt").write_bytes(b"tampered bytes")
    target = security_harness.out_dir / "tampered-bundle.zip"
    with pytest.raises(ReceiptExportError, match="mismatch|differ"):
        security_harness.exporter.export(
            security_harness.receipt.receipt_id,
            target,
            redaction="public",
            bundle_artifacts=True,
        )
    assert not target.exists()


def test_export_preserves_formula_like_strings_as_inert_json(
    tmp_path, store
):
    receipt = _make_receipt(
        source_id="s1:t7",
        turn_id="t7",
        statement='=HYPERLINK("http://example.com/x") + @SUM(A1:A2)',
    )
    store.insert(receipt)
    exporter = ReceiptExporter(store)
    exported = exporter.export(
        receipt.receipt_id, tmp_path / "formula.json", redaction="public"
    )
    data = json.loads(exported.read_text("utf-8"))
    assert data["receipt"]["claims"][0]["statement"] == (
        '=HYPERLINK("http://example.com/x") + @SUM(A1:A2)'
    )
    assert verify_export_hashes(exported)


def test_export_unknown_receipt_fails_closed(tmp_path, store):
    exporter = ReceiptExporter(store)
    with pytest.raises(ReceiptExportError, match="unknown receipt"):
        exporter.export(
            "rct_" + "0" * 64, tmp_path / "missing.json", redaction="public"
        )


# =========================================================================
# Signing
# =========================================================================


def test_valid_signature_never_promotes_unverified(signing_service, completed_receipt):
    attestation = signing_service.sign(completed_receipt)
    assert signing_service.verify(attestation).valid
    assert signing_service.store.get(completed_receipt.receipt_id).status == "completed_unverified"


def test_unconfigured_signer_is_not_loaded(signer_factory, config):
    config["receipts"]["signing"] = {"provider": "", "required": False}
    ReceiptSigningService.from_config(config)
    assert signer_factory.calls == 0


def test_signature_is_an_appended_immutable_attestation(
    signing_service, completed_receipt, store
):
    attestation = signing_service.sign(completed_receipt)
    stored = store.list_attestations(completed_receipt.receipt_id)
    assert stored == (attestation,)
    assert attestation.target_content_hash == completed_receipt.content_hash
    # Replaying the identical signature appends nothing new.
    assert signing_service.sign(completed_receipt) == attestation
    assert store.list_attestations(completed_receipt.receipt_id) == (attestation,)


def _self_consistent(attestation: ReceiptAttestation, **changes) -> ReceiptAttestation:
    """Rebuild an attestation with tampered fields and a matching hash."""
    body = {
        "target_kind": attestation.target_kind,
        "target_id": attestation.target_id,
        "target_content_hash": attestation.target_content_hash,
        "provider_id": attestation.provider_id,
        "key_id": attestation.key_id,
        "algorithm": attestation.algorithm,
        "signature_b64": attestation.signature_b64,
        "signed_at": attestation.signed_at,
        "verification_state": attestation.verification_state,
    }
    body.update(changes)
    content_hash = canonical_content_hash(body)
    return ReceiptAttestation(
        attestation_id="att_" + hash_hex(content_hash),
        content_hash=content_hash,
        **body,
    )


def test_forged_signature_bytes_fail_verification(
    signing_service, completed_receipt
):
    attestation = signing_service.sign(completed_receipt)
    forged = _self_consistent(
        attestation,
        signature_b64=base64.b64encode(b"forged-bytes").decode("ascii"),
    )
    result = signing_service.verify(forged)
    assert not result.valid
    # Tampered content without a recomputed hash is also rejected.
    inconsistent = replace(
        attestation,
        signature_b64=base64.b64encode(b"forged-bytes").decode("ascii"),
    )
    assert not signing_service.verify(inconsistent).valid


def test_swapped_target_hash_fails_verification(
    signing_service, completed_receipt
):
    attestation = signing_service.sign(completed_receipt)
    swapped = _self_consistent(
        attestation, target_content_hash="sha256:" + "ab" * 32
    )
    assert not signing_service.verify(swapped).valid


def test_replayed_attestation_for_another_receipt_fails(
    signing_service, completed_receipt, store
):
    other = store.insert(_make_receipt(source_id="s1:t2", turn_id="t2"))
    attestation = signing_service.sign(completed_receipt)
    replayed = _self_consistent(attestation, target_id=other.receipt_id)
    assert not signing_service.verify(replayed).valid


def test_unavailable_provider_optional_warns_and_leaves_unsigned(
    store, completed_receipt, config, caplog
):
    config["receipts"]["signing"] = {"provider": "not-registered", "required": False}
    service = ReceiptSigningService.from_config(config, store=store)
    with caplog.at_level(logging.WARNING):
        assert service.sign(completed_receipt) is None
    assert any("sign" in r.message.lower() for r in caplog.records)
    assert store.list_attestations(completed_receipt.receipt_id) == ()
    assert store.get(completed_receipt.receipt_id).status == "completed_unverified"


def test_unavailable_provider_required_blocks_signed_export_not_status(
    store, completed_receipt, config, tmp_path
):
    config["receipts"]["signing"] = {"provider": "not-registered", "required": True}
    service = ReceiptSigningService.from_config(config, store=store)
    with pytest.raises(SigningUnavailableError):
        service.sign(completed_receipt)
    exporter = ReceiptExporter(store, signing=service)
    with pytest.raises(ReceiptExportError):
        exporter.export(
            completed_receipt.receipt_id,
            tmp_path / "signed.json",
            redaction="public",
            sign=True,
        )
    # Required signing gates the signed export only; truth is unchanged.
    assert store.get(completed_receipt.receipt_id).status == "completed_unverified"


def test_plugin_check_fn_false_prevents_load(store, config):
    factory = _CountingFactory()
    register_receipt_signer("gated-plugin", factory, lambda cfg: False)
    try:
        config["receipts"]["signing"] = {"provider": "gated-plugin", "required": False}
        service = ReceiptSigningService.from_config(config, store=store)
        assert factory.calls == 0
        assert not service.available
    finally:
        unregister_receipt_signer("gated-plugin")


def test_imported_attestation_requires_explicit_verification(
    signing_service, completed_receipt, store
):
    material = _HmacSigner().sign(completed_receipt.content_hash)
    body = {
        "target_kind": "receipt",
        "target_id": completed_receipt.receipt_id,
        "target_content_hash": completed_receipt.content_hash,
        "provider_id": "test-hmac",
        "key_id": material.key_id,
        "algorithm": material.algorithm,
        "signature_b64": material.signature_b64,
        "signed_at": NOW,
        "verification_state": "unverified_import",
    }
    content_hash = canonical_content_hash(body)
    imported = ReceiptAttestation(
        attestation_id="att_" + hash_hex(content_hash),
        content_hash=content_hash,
        **body,
    )
    stored = store.append_attestation(imported)
    assert stored.verification_state == "unverified_import"
    # Explicit verification succeeds without mutating the stored row.
    assert signing_service.verify(imported).valid
    assert store.list_attestations(completed_receipt.receipt_id)[-1] == stored


# =========================================================================
# Retention
# =========================================================================


def _service(store, **kwargs):
    kwargs.setdefault("retention_days", 365)
    kwargs.setdefault("locator_retention_days", 90)
    kwargs.setdefault("now", lambda: NOW)
    return ReceiptRetentionService(store, **kwargs)


def test_retention_plan_lists_expired_ids_and_excludes_recent(store):
    old = store.insert(_make_receipt(source_id="s1:old", turn_id="told",
                                     decided_at=OLD_DECIDED_AT))
    recent = store.insert(_make_receipt(source_id="s1:new", turn_id="tnew",
                                        decided_at=RECENT_DECIDED_AT))
    plan = _service(store).plan()
    assert plan.receipt_ids == (old.receipt_id,)
    assert recent.receipt_id not in plan.receipt_ids
    assert plan.blockers == ()
    assert plan.plan_hash.startswith("sha256:")


def test_recent_observation_keeps_expired_receipt(store):
    old = store.insert(_make_receipt(source_id="s1:obs", turn_id="tobs",
                                     decided_at=OLD_DECIDED_AT))
    observation = build_observation(
        receipt_id=old.receipt_id,
        previous_observation_id=None,
        status="failed",
        evidence=old.evidence,
        uncertainty=("artifact hash changed on recheck",),
        scorer_id="hades.receipts.default",
        scorer_version="1.0",
        observed_at=RECENT_DECIDED_AT,
    )
    store.append_observation(observation)
    plan = _service(store).plan()
    assert old.receipt_id not in plan.receipt_ids


def test_prune_requires_exact_plan_hash(store):
    old = store.insert(_make_receipt(source_id="s1:old", turn_id="told",
                                     decided_at=OLD_DECIDED_AT))
    service = _service(store)
    plan = service.plan()
    with pytest.raises(RetentionPlanMismatch):
        service.prune(plan.plan_id, "sha256:" + "0" * 64)
    # The refused prune deleted nothing.
    assert store.get(old.receipt_id) == old
    assert store.list_tombstones() == ()


def test_prune_deletes_with_tombstones_and_locators_first(
    store, db, catalog, home, signing_service
):
    artifact_dir = home / "receipt-artifacts"
    artifact_dir.mkdir()
    artifact_path = artifact_dir / "report.bin"
    artifact_path.write_bytes(b"old artifact payload")
    digest = catalog.register_path(
        artifact_path,
        source_kind="execute_code",
        source_ref="s1:told:call-1",
        allowed_roots=(home,),
    )
    old = store.insert(
        _make_receipt(
            source_id="s1:prune", turn_id="tprune",
            decided_at=OLD_DECIDED_AT, artifacts=(digest,),
        )
    )
    observation = build_observation(
        receipt_id=old.receipt_id,
        previous_observation_id=None,
        status="failed",
        evidence=old.evidence,
        scorer_id="hades.receipts.default",
        scorer_version="1.0",
        observed_at="2024-02-01T00:00:00Z",
    )
    store.append_observation(observation)
    attestation = signing_service.sign(old)
    assert attestation is not None

    service = _service(store, artifact_dir=artifact_dir)
    plan = service.plan()
    assert plan.receipt_ids == (old.receipt_id,)
    assert plan.artifact_location_ids
    result = service.prune(plan.plan_id, plan.plan_hash)
    assert result.deleted_receipts == 1
    assert result.deleted_observations == 1
    assert result.deleted_attestations == 1
    assert result.deleted_artifact_locations >= 1
    assert result.tombstones == 1

    assert store.get(old.receipt_id) is None
    assert store.observations(old.receipt_id) == ()
    assert store.list_attestations(old.receipt_id) == ()
    tombstones = store.list_tombstones()
    assert len(tombstones) == 1
    tombstone = tombstones[0]
    assert tombstone.receipt_id == old.receipt_id
    assert tombstone.receipt_content_hash == old.content_hash
    assert tombstone.source_kind == "turn"
    assert tombstone.source_id == "s1:prune"
    # The raw locator rows are gone and the byte payload inside the
    # configured receipt artifact directory was removed.
    count = db._execute_read(
        lambda conn: conn.execute(
            "SELECT COUNT(*) FROM artifact_locations WHERE artifact_id = ?",
            (digest.artifact_id,),
        ).fetchone()[0]
    )
    assert count == 0
    assert not artifact_path.exists()


def test_prune_never_deletes_bytes_outside_artifact_dir(
    store, catalog, home
):
    outside_dir = home / "elsewhere"
    outside_dir.mkdir()
    outside_path = outside_dir / "keep.bin"
    outside_path.write_bytes(b"bytes outside the receipt artifact dir")
    digest = catalog.register_path(
        outside_path,
        source_kind="execute_code",
        source_ref="s1:tkeep:call-1",
        allowed_roots=(home,),
    )
    store.insert(
        _make_receipt(
            source_id="s1:keepbytes", turn_id="tkeep",
            decided_at=OLD_DECIDED_AT, artifacts=(digest,),
        )
    )
    artifact_dir = home / "receipt-artifacts"
    artifact_dir.mkdir()
    service = _service(store, artifact_dir=artifact_dir)
    plan = service.plan()
    service.prune(plan.plan_id, plan.plan_hash)
    # Rows were pruned but the byte payload outside the configured
    # receipt artifact directory is never unlinked.
    assert outside_path.exists()
    assert store.get(plan.receipt_ids[0]) is None


def test_idempotent_prune_replays_safely(store):
    store.insert(_make_receipt(source_id="s1:idem", turn_id="tidem",
                               decided_at=OLD_DECIDED_AT))
    service = _service(store)
    plan = service.plan()
    first = service.prune(plan.plan_id, plan.plan_hash)
    assert first.deleted_receipts == 1
    second = service.prune(plan.plan_id, plan.plan_hash)
    assert second.deleted_receipts == 0
    assert second.tombstones == 0
    assert len(store.list_tombstones()) == 1


def test_prune_recomputes_plan_in_a_fresh_service(store):
    store.insert(_make_receipt(source_id="s1:fresh", turn_id="tfresh",
                               decided_at=OLD_DECIDED_AT))
    plan = _service(store).plan()
    # A different service instance (fresh process model) revalidates by
    # recomputation before deleting anything.
    result = _service(store).prune(plan.plan_id, plan.plan_hash)
    assert result.deleted_receipts == 1


def test_user_hold_blocks_plan_and_prune(store):
    old = store.insert(_make_receipt(source_id="s1:hold", turn_id="thold",
                                     decided_at=OLD_DECIDED_AT))
    holds: list[RetentionHold] = []
    service = _service(store, holds=lambda: list(holds))
    plan = service.plan()
    assert plan.receipt_ids == (old.receipt_id,)
    # A hold added between plan and prune refuses the prune outright.
    holds.append(RetentionHold(old.receipt_id, "user", "litigation hold"))
    with pytest.raises(RetentionHoldError):
        service.prune(plan.plan_id, plan.plan_hash)
    assert store.get(old.receipt_id) is not None
    # And a fresh plan lists the blocker instead of the receipt.
    blocked_plan = service.plan()
    assert old.receipt_id not in blocked_plan.receipt_ids
    assert any(
        b.receipt_id == old.receipt_id and b.kind == "user"
        for b in blocked_plan.blockers
    )


def test_active_mission_hold_blocks_retention(store, home):
    workflows_db = home / "workflows.db"
    import sqlite3

    conn = sqlite3.connect(workflows_db)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS missions (
            mission_id TEXT PRIMARY KEY,
            profile TEXT NOT NULL,
            objective TEXT NOT NULL,
            constraints_json TEXT NOT NULL,
            authority_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            authority_version INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL,
            verdict TEXT,
            receipt_id TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            terminal_at INTEGER
        )"""
    )
    conn.execute(
        "INSERT INTO missions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("m-active", "default", "obj", "[]", "{}", "{}", 1,
         "running", None, None, 1000, 1000, None),
    )
    conn.execute(
        "INSERT INTO missions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("m-done", "default", "obj", "[]", "{}", "{}", 1,
         "completed", "success", None, 1000, 2000, 2000),
    )
    conn.commit()
    conn.close()

    active = store.insert(
        _make_receipt(
            source_id="m-active", source_kind="mission",
            subject_kind="mission", session_id=None, turn_id=None,
            mission_id="m-active", decided_at=OLD_DECIDED_AT,
        )
    )
    done = store.insert(
        _make_receipt(
            source_id="m-done", source_kind="mission",
            subject_kind="mission", session_id=None, turn_id=None,
            mission_id="m-done", decided_at=OLD_DECIDED_AT,
        )
    )
    plan = _service(store, workflows_db_path=workflows_db).plan()
    assert done.receipt_id in plan.receipt_ids
    assert active.receipt_id not in plan.receipt_ids
    assert any(
        b.receipt_id == active.receipt_id and b.kind == "mission"
        for b in plan.blockers
    )


def test_active_transaction_hold_blocks_retention(store, db):
    # The real effect_transactions table (missions vertical slice) ships in
    # SCHEMA_SQL, with an enforced FK to agent_operations — seed parents
    # first, then rows in the exact live schema.
    db._execute_write(
        lambda conn: conn.executemany(
            "INSERT INTO agent_operations (operation_id, kind, state, "
            "effect_disposition, created_at, updated_at) "
            "VALUES (?, 'effect', 'confirmed', 'landed', 1000, 1000)",
            [("op1",), ("op2",)],
        )
    )
    db._execute_write(
        lambda conn: conn.executemany(
            "INSERT INTO effect_transactions (transaction_id, operation_id, "
            "mission_id, adapter_id, sequence_no, semantics_json, phase, "
            "depends_on_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                ("tx-open", "op1", "m1", "email", 1, "{}", "prepared", "[]",
                 1000, 1000),
                ("tx-done", "op2", "m1", "email", 2, "{}", "committed", "[]",
                 1000, 2000),
            ],
        )
    )
    open_receipt = store.insert(
        _make_receipt(
            source_id="tx-open", source_kind="transaction",
            subject_kind="transaction", session_id=None, turn_id=None,
            transaction_id="tx-open", decided_at=OLD_DECIDED_AT,
        )
    )
    done_receipt = store.insert(
        _make_receipt(
            source_id="tx-done", source_kind="transaction",
            subject_kind="transaction", session_id=None, turn_id=None,
            transaction_id="tx-done", decided_at=OLD_DECIDED_AT,
        )
    )
    plan = _service(store).plan()
    assert done_receipt.receipt_id in plan.receipt_ids
    assert open_receipt.receipt_id not in plan.receipt_ids
    assert any(
        b.receipt_id == open_receipt.receipt_id and b.kind == "transaction"
        for b in plan.blockers
    )


def test_retention_never_crosses_profile_boundary(tmp_path, monkeypatch):
    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    home_a.mkdir()
    home_b.mkdir()
    monkeypatch.setenv("HADES_HOME", str(home_a))
    db_a = SessionDB(db_path=home_a / "state.db")
    db_b = SessionDB(db_path=home_b / "state.db")
    try:
        store_a = ReceiptStore(db_a)
        store_b = ReceiptStore(db_b)
        store_a.insert(_make_receipt(source_id="a:old", turn_id="ta",
                                     decided_at=OLD_DECIDED_AT))
        other = store_b.insert(_make_receipt(source_id="b:old", turn_id="tb",
                                             decided_at=OLD_DECIDED_AT))
        service = _service(store_a)
        plan = service.plan()
        service.prune(plan.plan_id, plan.plan_hash)
        # Profile B's receipt survives untouched — nothing crossed homes.
        assert store_b.get(other.receipt_id) == other
        assert store_b.list_tombstones() == ()
    finally:
        db_a.close()
        db_b.close()
