"""Task 10 secondary READ-ONLY Dashboard receipt inspection tests.

Real-path invariants against per-test profile homes under the isolated
``HADES_HOME`` root:

- ``GET /api/receipts``, ``GET /api/receipts/{receipt_id}``, and
  ``GET /api/receipts/{receipt_id}/observations`` are profile-aware:
  each read opens ONLY the resolved profile's ``state.db``;
- a token-authenticated caller whose principal carries a
  ``profile:<name>`` scope is bound to that profile — another profile's
  receipt IDs resolve to the same 404 a missing receipt gets, so a
  scoped caller can never confirm another profile's receipts exist;
- responses are redacted through the Task 7 redaction layer: no
  profile-home path prefix, no raw ``artifact_locations`` locator, no
  signer secret ever reaches the wire;
- list accepts only canonical status / subject-kind filters, a bounded
  cursor, and ``limit`` 1..200; detail/observations validate bounded
  receipt IDs;
- attestations are labeled "provenance only" and the original decision
  is distinguished from the latest recheck observation;
- the surface is inspection-only: no recheck/prune/sign/export route
  exists, mutating verbs are refused, and the read path never wires the
  signing, retention, or issuance services.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    from starlette.testclient import TestClient
except ImportError:  # pragma: no cover - environment guard
    pytest.skip("fastapi/starlette not installed", allow_module_level=True)

from hades_state import SessionDB
from agent.receipt_hashing import canonical_content_hash
from agent.receipt_models import (
    _VERIFIED_DECISION_CAPABILITY,
    _build_verified_decision,
    build_artifact_digest,
    build_claim,
    build_evidence_digest,
    build_observation,
    build_receipt,
    build_requested_outcome,
)
from agent.receipt_store import ReceiptAttestation, ReceiptStore
from agent.receipts import ReceiptSourceKey

RECENT_DECIDED_AT = "2026-07-10T00:00:00Z"
RECHECK_OBSERVED_AT = "2026-07-11T09:00:00Z"


# =========================================================================
# Receipt seeding helpers (real stores in real per-profile state.db files)
# =========================================================================


def _make_receipt(
    home: Path,
    *,
    source_id: str,
    status: str = "completed_unverified",
    scorer_id: str = "hades.receipts.default",
    subject_kind: str = "turn",
):
    """One full receipt whose evidence/artifacts embed the profile home
    path, so an unredacted response would leak it."""
    home_posix = home.as_posix()
    artifact = build_artifact_digest(
        source_kind="code_execution",
        source_ref=f"{home_posix}/artifacts/report.md",
        display_name="report.md",
        media_type="text/markdown",
        size_bytes=42,
        sha256="a" * 64,
        captured_at=RECENT_DECIDED_AT,
    )
    evidence = build_evidence_digest(
        evidence_kind="verification_check",
        source_ref=f"{home_posix}/verification_evidence.db:check:{source_id}",
        producer_id="hades.verification",
        observed_at=RECENT_DECIDED_AT,
        summary=f"pytest ran after final edit under {home_posix}",
        payload_hash=canonical_content_hash({"check": "pytest", "id": source_id}),
        artifact_ids=(artifact.artifact_id,),
    )
    claim = build_claim(
        statement="README contains marker",
        evidence_ids=(evidence.evidence_id,),
        artifact_ids=(artifact.artifact_id,),
        verdict="satisfied",
    )
    outcome = build_requested_outcome(
        outcome_kind="code_change",
        description="add marker to README",
        constraints=("no force push",),
        producer_id="hades.turn-ledger",
    )
    return build_receipt(
        source=ReceiptSourceKey("turn", source_id),
        subject_kind=subject_kind,
        subject_id=source_id,
        session_id="s1",
        turn_id=source_id,
        requested_outcome=outcome,
        status=status,
        claims=(claim,),
        evidence=(evidence,),
        artifacts=(artifact,),
        uncertainty=("recheck depends on the artifact still existing",),
        scorer_id=scorer_id,
        scorer_version="1.0",
        decided_at=RECENT_DECIDED_AT,
    )


def _seal_for(receipt):
    return _build_verified_decision(
        _VERIFIED_DECISION_CAPABILITY,
        scorer_id=receipt.scorer_id,
        scorer_version=receipt.scorer_version,
        subject_kind=receipt.subject_kind,
        subject_id=receipt.subject_id,
        snapshot_hash=canonical_content_hash({"snapshot": receipt.subject_id}),
        claim_hashes=tuple(c.content_hash for c in receipt.claims),
        decided_at=receipt.decided_at,
        fresh_until=None,
    )


def _make_attestation(receipt) -> ReceiptAttestation:
    body = {
        "target_kind": "receipt",
        "target_id": receipt.receipt_id,
        "target_content_hash": receipt.content_hash,
        "provider_id": "test-signer",
        "key_id": "k1",
        "algorithm": "hmac-sha256",
        "signature_b64": base64.b64encode(b"signature-bytes").decode("ascii"),
        "signed_at": RECENT_DECIDED_AT,
        "verification_state": "unverified_import",
    }
    digest = canonical_content_hash(body)
    return ReceiptAttestation(
        attestation_id=f"att_{digest.removeprefix('sha256:')}",
        content_hash=digest,
        **body,
    )


def _seed_profile(home: Path) -> dict:
    """Seed one profile store: a verified original with a failed recheck
    plus a plain completed_unverified receipt. Returns the seeded IDs."""
    db = SessionDB(db_path=home / "state.db")
    try:
        store = ReceiptStore(db)
        original = _make_receipt(
            home,
            source_id="s1:t7",
            status="verified",
            scorer_id="hades.code-turn-end-state",
        )
        stored = store.insert(original, decision=_seal_for(original))
        observation = build_observation(
            receipt_id=stored.receipt_id,
            previous_observation_id=None,
            status="failed",
            uncertainty=(
                "Artifact hash changed after issuance: report.md sha256 drifted",
            ),
            scorer_id="hades.code-turn-end-state",
            scorer_version="1.0",
            observed_at=RECHECK_OBSERVED_AT,
        )
        stored_observation = store.append_observation(observation)
        store.append_attestation(_make_attestation(stored))
        second = store.insert(_make_receipt(home, source_id="s1:t8"))
        second_observation = store.append_observation(
            build_observation(
                receipt_id=second.receipt_id,
                previous_observation_id=None,
                status="failed",
                uncertainty=(
                    "Artifact hash changed after issuance: report.md sha256 drifted",
                ),
                scorer_id="hades.code-turn-end-state",
                scorer_version="1.0",
                observed_at=RECHECK_OBSERVED_AT,
            )
        )
        store.append_attestation(_make_attestation(second))
        return {
            "receipt_id": stored.receipt_id,
            "content_hash": stored.content_hash,
            "observation_id": stored_observation.observation_id,
            "second_receipt_id": second.receipt_id,
            "second_content_hash": second.content_hash,
            "second_observation_id": second_observation.observation_id,
        }
    finally:
        db.close()


# =========================================================================
# Dashboard client + per-profile auth fixtures
# =========================================================================


ALPHA_TOKEN = "alpha-inspection-token"
BETA_TOKEN = "beta-inspection-token"


def _make_token_provider():
    from hades_cli.dashboard_auth.base import (
        DashboardAuthProvider,
        TokenPrincipal,
    )

    class _ProfileTokenProvider(DashboardAuthProvider):
        """Test-only bearer-token provider minting profile-scoped principals."""

        name = "receipt-profile-token"
        display_name = "Receipt profile token (test)"
        supports_token = True
        supports_session = False

        _TOKENS = {ALPHA_TOKEN: "alpha", BETA_TOKEN: "beta"}

        def start_login(self, *, redirect_uri):
            raise NotImplementedError

        def complete_login(self, *, code, state, code_verifier, redirect_uri):
            raise NotImplementedError

        def verify_session(self, *, access_token):
            return None

        def refresh_session(self, *, refresh_token):
            raise NotImplementedError

        def revoke_session(self, *, refresh_token):
            return None

        def verify_token(self, *, token):
            profile = self._TOKENS.get(token)
            if profile is None:
                return None
            return TokenPrincipal(
                principal=f"{profile}-inspector",
                provider=self.name,
                scopes=(f"profile:{profile}",),
            )

    return _ProfileTokenProvider()


@pytest.fixture()
def profiles():
    """Two isolated profiles (alpha seeded, beta empty) plus per-profile
    bearer-token auth headers bound to each profile by principal scope."""
    from hades_cli.dashboard_auth import clear_providers, register_provider
    from hades_cli.dashboard_auth.token_auth import (
        clear_token_routes,
        register_token_route,
    )

    root = Path(os.environ["HADES_HOME"])
    alpha_home = root / "profiles" / "alpha"
    beta_home = root / "profiles" / "beta"
    alpha_home.mkdir(parents=True)
    beta_home.mkdir(parents=True)

    seeded = _seed_profile(alpha_home)
    # Beta exists with an empty (but real) receipt store.
    SessionDB(db_path=beta_home / "state.db").close()

    clear_providers()
    clear_token_routes()
    register_provider(_make_token_provider())
    # Exact-path registration is the token seam's contract; register the
    # detail/observation paths this module exercises via bearer tokens.
    register_token_route(f"/api/receipts/{seeded['receipt_id']}")
    register_token_route(f"/api/receipts/{seeded['receipt_id']}/observations")

    yield SimpleNamespace(
        alpha=SimpleNamespace(
            name="alpha",
            home=alpha_home,
            auth_headers={"Authorization": f"Bearer {ALPHA_TOKEN}"},
            receipt_id=seeded["receipt_id"],
            content_hash=seeded["content_hash"],
            observation_id=seeded["observation_id"],
            second_receipt_id=seeded["second_receipt_id"],
            second_content_hash=seeded["second_content_hash"],
            second_observation_id=seeded["second_observation_id"],
        ),
        beta=SimpleNamespace(
            name="beta",
            home=beta_home,
            auth_headers={"Authorization": f"Bearer {BETA_TOKEN}"},
        ),
    )

    clear_providers()
    clear_token_routes()


@pytest.fixture()
def client():
    """Bare dashboard TestClient — auth is supplied per request."""
    from hades_cli.web_server import app

    return TestClient(app)


@pytest.fixture()
def session_client():
    """TestClient carrying the loopback dashboard session token."""
    from hades_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    inner = TestClient(app)
    inner.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return inner


# =========================================================================
# Plan-specified RED test
# =========================================================================


def test_dashboard_receipt_detail_is_profile_scoped_and_redacted(client, profiles):
    response = client.get(
        f"/api/receipts/{profiles.alpha.receipt_id}",
        headers=profiles.alpha.auth_headers,
    )
    assert response.status_code == 200
    assert response.json()["receipt_id"] == profiles.alpha.receipt_id
    assert profiles.alpha.home.as_posix() not in response.text
    denied = client.get(
        f"/api/receipts/{profiles.alpha.receipt_id}",
        headers=profiles.beta.auth_headers,
    )
    assert denied.status_code == 404


# =========================================================================
# Auth boundaries
# =========================================================================


def test_receipt_routes_require_auth(client, profiles):
    # No credentials at all → 401 from the session gate.
    assert client.get("/api/receipts").status_code == 401
    # A registered token route accepts ONLY bearer-token auth: neither a
    # missing token nor a garbage token passes.
    assert (
        client.get(f"/api/receipts/{profiles.alpha.receipt_id}").status_code
        == 401
    )
    assert (
        client.get(
            f"/api/receipts/{profiles.alpha.receipt_id}",
            headers={"Authorization": "Bearer wrong-token"},
        ).status_code
        == 401
    )


def test_profile_scoped_token_refuses_mismatching_profile_param(client, profiles):
    # A caller scoped to beta cannot pivot to alpha via ?profile= — the
    # refusal is the same 404 a missing receipt gets (no existence leak).
    denied = client.get(
        f"/api/receipts/{profiles.alpha.receipt_id}?profile=alpha",
        headers=profiles.beta.auth_headers,
    )
    assert denied.status_code == 404
    # The alpha-scoped caller may name its own profile explicitly.
    allowed = client.get(
        f"/api/receipts/{profiles.alpha.receipt_id}?profile=alpha",
        headers=profiles.alpha.auth_headers,
    )
    assert allowed.status_code == 200


# =========================================================================
# List: filters, validation, pagination, profile scoping
# =========================================================================


def test_list_is_profile_scoped_and_filterable(session_client, profiles):
    body = session_client.get("/api/receipts?profile=alpha").json()
    ids = {summary["receipt_id"] for summary in body["receipts"]}
    assert profiles.alpha.receipt_id in ids
    assert profiles.alpha.second_receipt_id in ids

    verified_only = session_client.get(
        "/api/receipts?profile=alpha&status=verified"
    ).json()
    assert {s["status"] for s in verified_only["receipts"]} == {"verified"}
    assert {s["receipt_id"] for s in verified_only["receipts"]} == {
        profiles.alpha.receipt_id
    }

    # Beta's store is real but empty — alpha receipts never bleed through.
    beta_body = session_client.get("/api/receipts?profile=beta").json()
    assert beta_body["receipts"] == []


def test_list_rejects_non_canonical_filters_and_bad_limits(session_client, profiles):
    assert (
        session_client.get(
            "/api/receipts?profile=alpha&status=success"
        ).status_code
        == 400
    )
    assert (
        session_client.get(
            "/api/receipts?profile=alpha&subject=workflow"
        ).status_code
        == 400
    )
    assert (
        session_client.get("/api/receipts?profile=alpha&limit=0").status_code
        == 400
    )
    assert (
        session_client.get("/api/receipts?profile=alpha&limit=201").status_code
        == 400
    )
    assert (
        session_client.get(
            "/api/receipts?profile=alpha&cursor=not-a-cursor"
        ).status_code
        == 400
    )


def test_list_paginates_with_cursor(session_client, profiles):
    first = session_client.get("/api/receipts?profile=alpha&limit=1").json()
    assert len(first["receipts"]) == 1
    assert first["next_cursor"]
    second = session_client.get(
        f"/api/receipts?profile=alpha&limit=1&cursor={first['next_cursor']}"
    ).json()
    assert len(second["receipts"]) == 1
    assert (
        second["receipts"][0]["receipt_id"]
        != first["receipts"][0]["receipt_id"]
    )


def test_unknown_profile_is_404_and_invalid_profile_400(session_client, profiles):
    assert session_client.get("/api/receipts?profile=nope").status_code == 404
    assert (
        session_client.get("/api/receipts?profile=..%2F..").status_code == 400
    )


# =========================================================================
# Detail: truthful structure and redaction
# =========================================================================


def test_detail_carries_claims_artifacts_observations_and_provenance(
    session_client, profiles
):
    # The second receipt's exact path is NOT a registered token route, so
    # the ordinary session-token dashboard auth applies.
    response = session_client.get(
        f"/api/receipts/{profiles.alpha.second_receipt_id}?profile=alpha"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["receipt"]["content_hash"] == profiles.alpha.second_content_hash
    # Original decision vs latest recheck are both present and distinct.
    assert body["receipt"]["status"] == "completed_unverified"
    assert body["latest_observation"]["status"] == "failed"
    assert body["latest_observation"]["observed_at"] == RECHECK_OBSERVED_AT
    assert body["observation_count"] == 1
    # Claim → evidence → artifact edges are traceable.
    edge = body["claim_edges"][0]
    assert edge["evidence_ids"]
    assert edge["artifact_ids"]
    assert body["receipt"]["artifacts"][0]["sha256"] == "a" * 64
    # Freshness and uncertainty are surfaced, not hidden.
    assert body["receipt"]["decided_at"] == RECENT_DECIDED_AT
    assert body["receipt"]["uncertainty"]
    # Signatures are provenance only — never a truth claim.
    assert body["attestations"][0]["role"] == "provenance only"
    assert body["attestations"][0]["verification_state"] == "unverified_import"
    # The page's primary-control hint points at the CLI recheck.
    assert (
        f"hades receipt recheck {profiles.alpha.second_receipt_id}"
        in body["recheck_hint"]
    )
    # Redaction: no profile-home prefix and no raw locator table leakage.
    assert profiles.alpha.home.as_posix() not in response.text
    assert "artifact_locations" not in response.text


def test_observations_endpoint_lists_append_only_history(session_client, profiles):
    response = session_client.get(
        f"/api/receipts/{profiles.alpha.second_receipt_id}/observations"
        "?profile=alpha"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["receipt_id"] == profiles.alpha.second_receipt_id
    assert [o["observation_id"] for o in body["observations"]] == [
        profiles.alpha.second_observation_id
    ]
    assert body["observations"][0]["status"] == "failed"
    assert profiles.alpha.home.as_posix() not in response.text


def test_unknown_and_malformed_receipt_ids_are_404(session_client, profiles):
    missing = "rct_" + "0" * 64
    assert (
        session_client.get(f"/api/receipts/{missing}?profile=alpha").status_code
        == 404
    )
    assert (
        session_client.get(
            f"/api/receipts/{missing}/observations?profile=alpha"
        ).status_code
        == 404
    )
    # Oversized / non-canonical IDs are refused without a traceback.
    oversized = "x" * 300
    response = session_client.get(f"/api/receipts/{oversized}?profile=alpha")
    assert response.status_code == 404
    assert "Traceback" not in response.text


# =========================================================================
# Read-only guarantees
# =========================================================================


def test_mutating_verbs_and_action_routes_are_refused(session_client, profiles):
    detail = f"/api/receipts/{profiles.alpha.second_receipt_id}"
    assert session_client.post(detail).status_code == 405
    assert session_client.delete(detail).status_code == 405
    assert session_client.put(detail).status_code == 405
    assert session_client.post("/api/receipts").status_code == 405
    # No recheck/prune/sign/export routes exist on the dashboard: the
    # only route under a receipt is GET .../observations, so an action
    # POST is refused (404 unknown route, or 405 where only the SPA
    # catch-all GET matches the path) and never reaches a handler.
    for action in ("recheck", "prune", "sign", "export"):
        assert session_client.post(f"{detail}/{action}").status_code in (
            404,
            405,
        ), action
    # Nothing above appended an observation.
    body = session_client.get(f"{detail}/observations?profile=alpha").json()
    assert len(body["observations"]) == 1


def test_read_paths_never_wire_signing_retention_or_issuance(
    session_client, profiles, monkeypatch
):
    import agent.receipt_ingest as receipt_ingest
    import agent.receipt_security as receipt_security

    def _boom(*_args, **_kwargs):
        raise AssertionError(
            "read-only dashboard inspection must not wire mutating services"
        )

    monkeypatch.setattr(
        receipt_security.ReceiptSigningService, "from_config", _boom
    )
    monkeypatch.setattr(receipt_security, "ReceiptRetentionService", _boom)
    monkeypatch.setattr(receipt_security, "ReceiptExporter", _boom)
    monkeypatch.setattr(
        receipt_ingest, "build_receipt_issuer", _boom, raising=False
    )

    assert session_client.get("/api/receipts?profile=alpha").status_code == 200
    assert (
        session_client.get(
            f"/api/receipts/{profiles.alpha.second_receipt_id}?profile=alpha"
        ).status_code
        == 200
    )
    assert (
        session_client.get(
            f"/api/receipts/{profiles.alpha.second_receipt_id}"
            "/observations?profile=alpha"
        ).status_code
        == 200
    )
