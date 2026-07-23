"""Task 9 native ``receipt.exec`` JSON-RPC tests for ``tui_gateway.server``.

Real-path invariants against the per-test ``HADES_HOME``:

- ``receipt.exec`` is a bounded live-process RPC over the shared
  ``hades_cli.receipts.run_argv`` surface (no shell, no subprocess);
- the response is structured and traceable: the canonical ``receipt``
  content hash, ``claim_edges`` with evidence/artifact IDs, recheck
  ``observations``, and the shared truthful text rendering in
  ``output`` (original vs latest recheck distinction included);
- argv is validated (non-empty list[str], at most 64 entries, at most
  64 KiB total UTF-8) and violations are refused with JSON-RPC 4004
  WITHOUT echoing any argument content — a secret-bearing oversized
  argument never round-trips through the error message;
- validation/unknown-ID failures map to JSON-RPC 4xxx and storage or
  signing-provider failures to 5xxx, never leaking a traceback;
- recheck appends an immutable linked observation and never rewrites
  the original receipt;
- a session carrying ``profile_home`` resolves THAT profile's
  ``state.db``; a caller-supplied path in params is never accepted.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hades_state import SessionDB
from agent.receipt_hashing import canonical_content_hash
from agent.receipt_models import (
    RECEIPT_STATUSES,
    _VERIFIED_DECISION_CAPABILITY,
    _build_verified_decision,
    build_claim,
    build_evidence_digest,
    build_observation,
    build_receipt,
    build_requested_outcome,
)
from agent.receipt_store import ReceiptStore
from agent.receipts import ReceiptSourceKey

RECENT_DECIDED_AT = "2026-07-10T00:00:00Z"


# =========================================================================
# Receipt fixtures (real store in the per-test HADES_HOME state.db)
# =========================================================================


@pytest.fixture()
def home() -> Path:
    """The per-test profile home the RPC resolves via ``get_hades_home``."""
    return Path(os.environ["HADES_HOME"])


@pytest.fixture()
def db(home):
    session_db = SessionDB(db_path=home / "state.db")
    yield session_db
    session_db.close()


@pytest.fixture()
def store(db):
    return ReceiptStore(db)


def _make_receipt(
    *,
    source_id: str = "s1:t1",
    session_id: str | None = "s1",
    turn_id: str | None = "t1",
    status: str = "completed_unverified",
    verdict: str = "satisfied",
    decided_at: str = RECENT_DECIDED_AT,
    statement: str = "README contains marker",
    scorer_id: str = "hades.receipts.default",
    uncertainty: tuple[str, ...] = (),
):
    evidence = build_evidence_digest(
        evidence_kind="verification_check",
        source_ref=f"verification_evidence.db:check:{source_id}",
        producer_id="hades.verification",
        observed_at=decided_at,
        summary="pytest ran after final edit",
        payload_hash=canonical_content_hash({"check": "pytest", "id": source_id}),
    )
    claim = build_claim(
        statement=statement,
        evidence_ids=(evidence.evidence_id,),
        verdict=verdict,
    )
    outcome = build_requested_outcome(
        outcome_kind="code_change",
        description="add marker to README",
        constraints=("no force push",),
        producer_id="hades.turn-ledger",
    )
    return build_receipt(
        source=ReceiptSourceKey("turn", source_id),
        subject_kind="turn",
        subject_id=source_id,
        session_id=session_id,
        turn_id=turn_id,
        requested_outcome=outcome,
        status=status,
        claims=(claim,),
        evidence=(evidence,),
        uncertainty=uncertainty,
        scorer_id=scorer_id,
        scorer_version="1.0",
        decided_at=decided_at,
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


@pytest.fixture()
def seeded_receipt(store):
    return store.insert(_make_receipt())


@pytest.fixture()
def receipt_with_drift(store):
    """A verified original whose latest recheck truthfully failed."""
    original = _make_receipt(
        source_id="s1:t7",
        turn_id="t7",
        status="verified",
        scorer_id="hades.code-turn-end-state",
    )
    stored = store.insert(original, decision=_seal_for(original))
    observation = build_observation(
        receipt_id=stored.receipt_id,
        previous_observation_id=None,
        status="failed",
        uncertainty=(
            "Artifact hash changed after issuance: README.md sha256 drifted",
        ),
        scorer_id="hades.code-turn-end-state",
        scorer_version="1.0",
        observed_at="2026-07-11T09:00:00Z",
    )
    store.append_observation(observation)
    return stored


# =========================================================================
# Gateway fixtures
# =========================================================================


@pytest.fixture()
def server():
    with patch.dict(
        "sys.modules",
        {
            "hades_cli.env_loader": MagicMock(),
            "hades_cli.banner": MagicMock(),
        },
    ):
        mod = importlib.import_module("tui_gateway.server")
        yield mod
        # See test_goal_command.py: never importlib.reload here — clear the
        # per-session dicts instead so atexit hooks stay single-registered.
        mod._sessions.clear()
        mod._pending.clear()
        mod._answers.clear()


@pytest.fixture()
def rpc(server):
    """Call one registered RPC; success returns ``result``, errors the
    raw JSON-RPC envelope (so tests can assert ``result["error"]``)."""

    def call(method: str, params: dict) -> dict:
        resp = server._methods[method](1, params)
        return resp["result"] if "result" in resp else resp

    return call


# =========================================================================
# Plan-specified RED tests
# =========================================================================


def test_receipt_rpc_returns_traceable_detail(rpc, seeded_receipt):
    result = rpc("receipt.exec", {
        "session_id": "sid", "argv": ["show", seeded_receipt.receipt_id]
    })
    assert result["ok"] is True
    assert result["receipt"]["content_hash"] == seeded_receipt.content_hash
    assert result["claim_edges"][0]["evidence_ids"]


def test_receipt_rpc_rejects_oversized_argv_without_secret_echo(rpc):
    result = rpc("receipt.exec", {"session_id": "sid", "argv": ["x" * 65537]})
    assert result["error"]["code"] == 4004
    assert "x" * 100 not in result["error"]["message"]


# =========================================================================
# Structured, profile-local success paths
# =========================================================================


def test_list_returns_summaries_and_shared_text_rendering(rpc, seeded_receipt):
    result = rpc("receipt.exec", {"session_id": "sid", "argv": ["list"]})
    assert result["ok"] is True
    assert result["action"] == "list"
    ids = [summary["receipt_id"] for summary in result["receipts"]]
    assert seeded_receipt.receipt_id in ids
    statuses = {summary["status"] for summary in result["receipts"]}
    assert statuses <= RECEIPT_STATUSES
    # The shared truthful renderer's text rides along for the Ink pager.
    assert seeded_receipt.receipt_id in result["output"]
    assert "success" not in result["output"].lower()


def test_show_distinguishes_original_from_latest_recheck(rpc, receipt_with_drift):
    result = rpc(
        "receipt.exec",
        {"session_id": "sid", "argv": ["show", receipt_with_drift.receipt_id]},
    )
    assert result["ok"] is True
    assert result["action"] == "show"
    assert "Original: verified" in result["output"]
    assert "Latest recheck: failed" in result["output"]
    assert result["observations"][0]["status"] == "failed"
    # Attestation section is always labeled provenance-only.
    assert "provenance only" in result["output"].lower()


def _turn_record(**overrides):
    from agent.turn_ledger import TurnOutcomeRecord

    fields = dict(
        session_id="s1",
        turn_id="t1",
        created_at=1752660000.0,
        outcome="completed_unverified",
        outcome_reason="response completed without verification",
        turn_exit_reason="text_response(finish_reason=stop)",
        api_calls=1,
        tool_iterations=1,
        retry_count=0,
        guardrail_halt=None,
        cost_usd_delta=0.0,
        input_tokens_delta=10,
        output_tokens_delta=5,
        cache_read_tokens_delta=0,
        skills_loaded=(),
        model="test-model",
    )
    fields.update(overrides)
    return TurnOutcomeRecord(**fields)


def test_recheck_appends_observation_and_never_rewrites_original(rpc, db, store):
    from agent.receipt_ingest import build_receipt_issuer

    db.record_turn_outcome(_turn_record())
    original = build_receipt_issuer(db).issue(ReceiptSourceKey("turn", "s1:t1"))
    result = rpc(
        "receipt.exec",
        {"session_id": "sid", "argv": ["recheck", original.receipt_id]},
    )
    assert result["ok"] is True
    assert result["action"] == "recheck"
    assert len(result["observations"]) == 1
    assert result["observations"][0]["status"] in RECEIPT_STATUSES
    # The original receipt is immutable — recheck only appended.
    assert store.get(original.receipt_id) == original
    assert len(store.observations(original.receipt_id)) == 1


def test_session_profile_home_override_is_honored(rpc, server, tmp_path):
    other_home = tmp_path / "other-profile"
    other_home.mkdir()
    other_db = SessionDB(db_path=other_home / "state.db")
    try:
        other = ReceiptStore(other_db).insert(
            _make_receipt(source_id="s2:t1", session_id="s2")
        )
    finally:
        other_db.close()
    server._sessions["sid-other"] = {
        "session_key": "tui-receipt-other",
        "profile_home": str(other_home),
    }
    result = rpc(
        "receipt.exec", {"session_id": "sid-other", "argv": ["list"]}
    )
    assert result["profile_home"] == str(other_home)
    assert [s["receipt_id"] for s in result["receipts"]] == [other.receipt_id]


def test_caller_supplied_profile_path_is_never_accepted(rpc, home, tmp_path, seeded_receipt):
    evil_home = tmp_path / "evil-profile"
    evil_home.mkdir()
    result = rpc(
        "receipt.exec",
        {
            "session_id": "sid",
            "argv": ["list"],
            "profile_home": str(evil_home),
            "home": str(evil_home),
        },
    )
    assert result["ok"] is True
    # Only the session registry may steer profile resolution.
    assert result["profile_home"] == str(home)
    assert [s["receipt_id"] for s in result["receipts"]] == [
        seeded_receipt.receipt_id
    ]


# =========================================================================
# Bounded argv validation (4004, never echoing content)
# =========================================================================


def test_argv_must_be_a_nonempty_list_of_strings(rpc):
    for bad in ("list", None, [], [1, 2], [["list"]], {"a": 1}):
        resp = rpc("receipt.exec", {"session_id": "sid", "argv": bad})
        assert resp["error"]["code"] == 4004, resp


def test_invalid_session_id_type_is_rejected_without_traceback(rpc):
    resp = rpc("receipt.exec", {"session_id": {}, "argv": ["list"]})

    assert resp["error"]["code"] == 4004
    assert "Traceback" not in resp["error"]["message"]
    assert "{}" not in resp["error"]["message"]


def test_argv_entry_count_is_bounded_to_64(rpc):
    resp = rpc("receipt.exec", {"session_id": "sid", "argv": ["list"] + ["x"] * 64})
    assert resp["error"]["code"] == 4004


# =========================================================================
# Error mapping and redaction
# =========================================================================


def test_unknown_receipt_id_maps_to_4xxx_without_traceback(rpc):
    resp = rpc(
        "receipt.exec",
        {"session_id": "sid", "argv": ["show", "rct_" + "f" * 64]},
    )
    assert 4000 <= resp["error"]["code"] < 5000
    assert "Traceback" not in resp["error"]["message"]


def test_storage_failures_map_to_5xxx(rpc, monkeypatch):
    import hades_cli.receipts as receipts_mod

    def storage_failure(argv, **kwargs):
        return receipts_mod.ReceiptCommandResult(
            receipts_mod.EXIT_STORAGE,
            "error: storage failure",
            {"ok": False, "error": "storage failure", "code": "storage_failure"},
        )

    monkeypatch.setattr(receipts_mod, "run_argv", storage_failure)
    resp = rpc("receipt.exec", {"session_id": "sid", "argv": ["list"]})
    assert 5000 <= resp["error"]["code"] < 6000
    assert "Traceback" not in resp["error"]["message"]


def test_signing_unavailable_maps_to_5xxx(rpc, monkeypatch):
    import hades_cli.receipts as receipts_mod

    def unavailable(argv, **kwargs):
        return receipts_mod.ReceiptCommandResult(
            receipts_mod.EXIT_UNAVAILABLE,
            "error: signing provider unavailable",
            {"ok": False, "error": "signing provider unavailable"},
        )

    monkeypatch.setattr(receipts_mod, "run_argv", unavailable)
    resp = rpc(
        "receipt.exec",
        {"session_id": "sid", "argv": ["export", "rct_" + "0" * 64,
                                       "--output", "r.json", "--sign"]},
    )
    assert 5000 <= resp["error"]["code"] < 6000


def test_unexpected_failures_map_to_5xxx_and_are_redacted(rpc, monkeypatch):
    import hades_cli.receipts as receipts_mod

    def boom(*_a, **_k):
        raise RuntimeError("secret-token-xyz leaked path C:/private")

    monkeypatch.setattr(receipts_mod, "run_argv", boom)
    resp = rpc("receipt.exec", {"session_id": "sid", "argv": ["list"]})
    assert 5000 <= resp["error"]["code"] < 6000
    assert "secret-token-xyz" not in resp["error"]["message"]
    assert "C:/private" not in resp["error"]["message"]
    assert "Traceback" not in resp["error"]["message"]


@pytest.mark.parametrize(
    ("exit_name", "error_code", "expected_message"),
    [
        (
            "EXIT_VALIDATION",
            4005,
            "receipt.exec: validation failed (details withheld; run hades receipt in terminal)",
        ),
        (
            "EXIT_UNAVAILABLE",
            5041,
            "receipt.exec: signing provider unavailable (details withheld; run hades receipt in terminal)",
        ),
        (
            "EXIT_STORAGE",
            5040,
            "receipt.exec: storage failure (details withheld; run hades receipt list in terminal)",
        ),
    ],
)
def test_cli_failure_payload_error_is_not_forwarded(
    rpc, monkeypatch, exit_name, error_code, expected_message
):
    import hades_cli.receipts as receipts_mod

    secret = "receipt-wire-secret-token"
    path = "/private/receipts/secret.db"
    details = f"{secret} {path}\nTraceback (most recent call last)"

    def failed(argv, **kwargs):
        return receipts_mod.ReceiptCommandResult(
            getattr(receipts_mod, exit_name),
            f"producer output: {details}",
            {"ok": False, "error": details, "code": "producer_failure"},
        )

    monkeypatch.setattr(receipts_mod, "run_argv", failed)
    resp = rpc("receipt.exec", {"session_id": "sid", "argv": ["list"]})
    error = resp["error"]
    message = str(error["message"])

    assert error["code"] == error_code
    assert message == expected_message
    assert len(message) <= 256
    assert secret not in str(resp)
    assert path not in str(resp)
    assert "Traceback" not in str(resp)


def test_exit_ok_false_payload_is_a_bounded_command_error(rpc, monkeypatch):
    import hades_cli.receipts as receipts_mod

    secret = "receipt-false-success-secret"
    path = "/private/receipts/false-success.db"
    details = f"{secret} {path}\nTraceback (most recent call last)"
    failed = receipts_mod.ReceiptCommandResult(
        receipts_mod.EXIT_OK,
        f"producer output: {details}",
        {"ok": False, "error": details},
    )
    monkeypatch.setattr(receipts_mod, "run_argv", lambda *_a, **_k: failed)

    resp = rpc("receipt.exec", {"session_id": "sid", "argv": ["list"]})

    assert resp["error"]["code"] == 5040
    assert secret not in str(resp)
    assert path not in str(resp)
    assert "Traceback" not in str(resp)


def test_exit_ok_failure_only_payload_error_is_a_bounded_command_error(
    rpc, monkeypatch
):
    import hades_cli.receipts as receipts_mod

    secret = "receipt-failure-only-secret"
    failed = receipts_mod.ReceiptCommandResult(
        receipts_mod.EXIT_OK, secret, {"error": secret}
    )
    monkeypatch.setattr(receipts_mod, "run_argv", lambda *_a, **_k: failed)

    resp = rpc("receipt.exec", {"session_id": "sid", "argv": ["list"]})

    assert resp["error"]["code"] == 5040
    assert secret not in str(resp)


@pytest.mark.parametrize("payload", ["not-a-dict", ["not", "a", "dict"]])
def test_malformed_payload_is_a_fixed_error(rpc, monkeypatch, payload):
    import hades_cli.receipts as receipts_mod

    result = receipts_mod.ReceiptCommandResult(receipts_mod.EXIT_OK, "safe", payload)
    monkeypatch.setattr(receipts_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc("receipt.exec", {"session_id": "sid", "argv": ["list"]})

    assert resp["error"]["code"] == 5043
    assert "-32000" not in str(resp)


def test_payload_dict_subclass_get_exception_is_not_on_wire(rpc, monkeypatch):
    import hades_cli.receipts as receipts_mod

    secret = "receipt-malicious-get-secret"

    class MaliciousPayload(dict):
        def get(self, *_args, **_kwargs):
            raise RuntimeError(f"{secret} /private/receipts\nTraceback")

    result = receipts_mod.ReceiptCommandResult(
        receipts_mod.EXIT_OK, "safe", MaliciousPayload(ok=True)
    )
    monkeypatch.setattr(receipts_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc("receipt.exec", {"session_id": "sid", "argv": ["list"]})

    assert resp["error"]["code"] == 5043
    assert secret not in str(resp)
    assert "/private/receipts" not in str(resp)
    assert "Traceback" not in str(resp)


def test_success_output_is_bounded_with_deterministic_suffix(rpc, monkeypatch):
    import hades_cli.receipts as receipts_mod

    suffix = "... [truncated]"
    output = "receipt output\n" + ("x" * 20_000)
    result = receipts_mod.ReceiptCommandResult(
        receipts_mod.EXIT_OK, output, {"ok": True, "action": "list"}
    )
    monkeypatch.setattr(receipts_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc("receipt.exec", {"session_id": "sid", "argv": ["list"]})

    assert resp["output"] == output[: 16_384 - len(suffix)] + suffix
    assert len(resp["output"]) <= 16_384


def test_success_envelope_is_bounded(rpc, monkeypatch):
    import hades_cli.receipts as receipts_mod

    result = receipts_mod.ReceiptCommandResult(
        receipts_mod.EXIT_OK,
        "short",
        {"ok": True, "receipts": [{"receipt_id": "r", "detail": "x" * 1_100_000}]},
    )
    monkeypatch.setattr(receipts_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc("receipt.exec", {"session_id": "sid", "argv": ["list"]})

    assert resp["error"]["code"] == 5043
    assert len(str(resp)) < 2_000
