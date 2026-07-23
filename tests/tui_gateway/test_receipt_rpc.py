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
import json
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


@pytest.mark.parametrize(
    ("method", "code"),
    [
        ("autonomy.exec", 4033),
        ("receipt.exec", 4004),
        ("transaction.exec", 4006),
    ],
)
@pytest.mark.parametrize(
    "rid",
    [
        pytest.param("r" * 1_100_000, id="oversized-json-id"),
        pytest.param(object(), id="non-json-id"),
    ],
)
def test_native_validation_errors_bound_oversized_or_non_json_ids(
    server, method, code, rid
):
    resp = server._methods[method](rid, {"argv": []})

    assert resp["error"]["code"] == code
    assert resp["id"] is None
    assert len((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8")) <= 1_048_576


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
        "cwd": str(other_home),
    }
    result = rpc(
        "receipt.exec", {"session_id": "sid-other", "argv": ["list"]}
    )
    assert "profile_home" not in result
    assert [s["receipt_id"] for s in result["receipts"]] == [other.receipt_id]


def test_receipt_rpc_binds_session_workspace_for_native_run_argv(
    rpc, server, tmp_path, monkeypatch
):
    import hades_cli.receipts as receipts_mod
    from hades_cli.workspace_context import get_workspace_root

    launch = tmp_path / "gateway-launch"
    workspace = tmp_path / "session-workspace"
    launch.mkdir()
    workspace.mkdir()
    server._sessions["sid-receipt-workspace"] = {
        "session_key": "tui-receipt-workspace",
        "cwd": str(workspace),
    }
    captured: list[Path] = []

    def run(argv, **kwargs):
        captured.append(get_workspace_root())
        return receipts_mod.ReceiptCommandResult(
            receipts_mod.EXIT_OK,
            "safe",
            {"ok": True, "action": "list", "receipts": []},
        )

    monkeypatch.setattr(receipts_mod, "run_argv", run)
    monkeypatch.chdir(launch)
    result = rpc("receipt.exec", {
        "session_id": "sid-receipt-workspace", "argv": ["list"],
    })

    assert result["ok"] is True
    assert captured == [workspace.resolve()]
    assert Path.cwd() == launch


@pytest.mark.parametrize("transition", ["pop", "replace"])
def test_receipt_rpc_keeps_native_profile_workspace_generation_paired(
    rpc, server, tmp_path, monkeypatch, transition
):
    import hades_cli.receipts as receipts_mod
    from hades_cli.workspace_context import get_workspace_root
    from hades_constants import get_hades_home

    launch = tmp_path / "gateway-launch"
    profile_a = tmp_path / "profile-a"
    workspace_a = profile_a / "workspace-a"
    profile_b = tmp_path / "profile-b"
    workspace_b = profile_b / "workspace-b"
    for path in (launch, workspace_a, workspace_b):
        path.mkdir(parents=True)
    sid = "sid-receipt-atomic-context"
    server._sessions[sid] = {
        "session_key": "tui-receipt-atomic-context-a",
        "profile_home": str(profile_a),
        "cwd": str(workspace_a),
    }

    def stale_workspace_lookup(_session_id):
        if transition == "pop":
            server._sessions.pop(sid, None)
            return launch.resolve()
        server._sessions[sid] = {
            "session_key": "tui-receipt-atomic-context-b",
            "profile_home": str(profile_b),
            "cwd": str(workspace_b),
        }
        return workspace_b.resolve()

    monkeypatch.setattr(server, "_native_workspace_for_session", stale_workspace_lookup)
    captured: list[tuple[Path, Path]] = []

    def run(argv, **kwargs):
        captured.append((Path(get_hades_home()).resolve(), get_workspace_root()))
        return receipts_mod.ReceiptCommandResult(
            receipts_mod.EXIT_OK,
            "safe",
            {"ok": True, "action": "list", "receipts": []},
        )

    monkeypatch.setattr(receipts_mod, "run_argv", run)
    monkeypatch.chdir(launch)
    response = rpc("receipt.exec", {"session_id": sid, "argv": ["list"]})

    assert response["ok"] is True
    assert captured == [(profile_a.resolve(), workspace_a.resolve())]
    assert Path.cwd() == launch


@pytest.mark.parametrize("cwd", [None, "", 17, "/private/missing-receipt-workspace"])
def test_receipt_rpc_fails_closed_for_invalid_registered_workspace(
    rpc, server, tmp_path, monkeypatch, cwd
):
    import hades_cli.receipts as receipts_mod

    launch = tmp_path / "gateway-launch"
    launch.mkdir()
    server._sessions["sid-receipt-invalid-workspace"] = {
        "session_key": "tui-receipt-invalid-workspace",
        "cwd": cwd,
    }
    called = False

    def run(*args, **kwargs):
        nonlocal called
        called = True
        return receipts_mod.ReceiptCommandResult(
            receipts_mod.EXIT_OK, "unsafe", {"ok": True}
        )

    monkeypatch.setattr(receipts_mod, "run_argv", run)
    monkeypatch.chdir(launch)
    response = rpc(
        "receipt.exec",
        {"session_id": "sid-receipt-invalid-workspace", "argv": ["list"]},
    )

    assert response["error"]["code"] == 5043
    assert "gateway-launch" not in str(response)
    assert "/private/missing-receipt-workspace" not in str(response)
    assert called is False


def test_receipt_rpc_fails_closed_when_registered_workspace_is_deleted(
    rpc, server, tmp_path, monkeypatch
):
    import hades_cli.receipts as receipts_mod

    launch = tmp_path / "gateway-launch"
    workspace = tmp_path / "deleted-receipt-workspace"
    launch.mkdir()
    workspace.mkdir()
    server._sessions["sid-receipt-deleted-workspace"] = {
        "session_key": "tui-receipt-deleted-workspace",
        "cwd": str(workspace),
    }
    workspace.rmdir()
    called = False

    def run(*args, **kwargs):
        nonlocal called
        called = True
        return receipts_mod.ReceiptCommandResult(
            receipts_mod.EXIT_OK, "unsafe", {"ok": True}
        )

    monkeypatch.setattr(receipts_mod, "run_argv", run)
    monkeypatch.chdir(launch)
    response = rpc(
        "receipt.exec",
        {"session_id": "sid-receipt-deleted-workspace", "argv": ["list"]},
    )

    assert response["error"]["code"] == 5043
    assert str(workspace) not in str(response)
    assert str(launch) not in str(response)
    assert called is False


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
    assert "profile_home" not in result
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


def test_success_frame_bound_includes_default_rpc_envelope_and_newline(
    rpc, home, monkeypatch
):
    import hades_cli.receipts as receipts_mod

    rows = [{
        "receipt_id": f"r{i:05d}",
        "status": "verified",
        "subject_id": f"s{i}",
        "subject_kind": "turn",
        "decided_at": "2026-07-10T00:00:00Z",
        "content_hash": f"sha256:{i}",
        "scorer_id": "scorer",
        "scorer_version": "1.0",
    } for i in range(5_200)]
    payload = {"ok": True, "action": "list", "receipts": rows}
    expected_inner = {
        "ok": True,
        "action": "list",
        "exit_code": receipts_mod.EXIT_OK,
        "output": "short",
        "receipts": rows,
    }
    compact_payload_bytes = len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    compact_inner_bytes = len(
        json.dumps(expected_inner, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    expected_frame = (
        json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": expected_inner},
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )
    cap = 1_048_576
    assert compact_payload_bytes < cap
    assert compact_inner_bytes < cap
    assert len(expected_frame) > cap

    result = receipts_mod.ReceiptCommandResult(receipts_mod.EXIT_OK, "short", payload)
    monkeypatch.setattr(receipts_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc("receipt.exec", {"session_id": "sid", "argv": ["list"]})

    assert resp["ok"] is True
    assert len(resp["receipts"]) <= 256
    final_frame = json.dumps({"jsonrpc": "2.0", "id": 1, "result": resp}, ensure_ascii=False).encode("utf-8") + b"\n"
    assert len(final_frame) <= cap


def test_valid_receipt_success_payload_forwards_only_minimal_nonlocator_fields(
    rpc, monkeypatch
):
    import hades_cli.receipts as receipts_mod

    secret = "sk-test-ReceiptValidSuccessSecret"
    private_path = "/Users/private/receipts/result.json"
    artifact_locator = f"artifact://private/{secret}"
    receipt_id = "rct_" + "a" * 64
    observation_id = "obs_" + "b" * 64
    result = receipts_mod.ReceiptCommandResult(
        receipts_mod.EXIT_OK,
        f"receipt output {secret} {private_path}",
        {
            "ok": True,
            "action": "show",
            "receipts": [{
                "receipt_id": receipt_id,
                "status": "verified",
                "subject_id": "s1:t1",
                "subject_kind": "turn",
                "decided_at": "2026-07-10T00:00:00Z",
                "content_hash": "sha256:deadbeef",
                "scorer_id": "scorer",
                "scorer_version": "1.0",
                "session_id": "s1",
                "source_ref": f"source_ref {private_path} {secret}",
            }],
            "receipt": {
                "receipt_id": receipt_id,
                "status": "verified",
                "subject_id": "s1:t1",
                "subject_kind": "turn",
                "content_hash": "sha256:deadbeef",
                "decided_at": "2026-07-10T00:00:00Z",
                "scorer_id": "scorer",
                "scorer_version": "1.0",
                "session_id": "s1",
                "requested_outcome": {
                    "description": f"outcome {secret} {private_path}",
                    "constraints": [f"constraint {secret}"],
                },
                "source_ref": f"source {private_path}",
                "evidence": [{"source_ref": f"evidence {private_path}"}],
                "artifacts": [{
                    "artifact_id": "art-1",
                    "source_ref": artifact_locator,
                    "display_name": f"artifact {secret}",
                }],
                "claims": [{
                    "claim_id": "claim-1",
                    "statement": f"claim {secret} {private_path}",
                    "uncertainty": [f"uncertainty {secret}"],
                }],
            },
            "observations": [{
                "observation_id": observation_id,
                "receipt_id": receipt_id,
                "status": "verified",
                "observed_at": "2026-07-11T09:00:00Z",
                "content_hash": "sha256:feedface",
                "source_ref": f"observation {private_path}",
                "evidence": [{"payload": secret}],
                "artifacts": [{"locator": artifact_locator}],
            }],
            "claim_edges": [{
                "claim_id": "claim-1",
                "verdict": "satisfied",
                "required": True,
                "statement": f"statement {secret} {private_path}",
                "uncertainty": [f"edge uncertainty {secret}"],
                "evidence_ids": ["evidence-1"],
                "artifact_ids": [artifact_locator],
            }],
            "export_path": private_path,
            "warning": f"warning {secret} {private_path}",
        },
    )
    monkeypatch.setattr(receipts_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc("receipt.exec", {"session_id": "sid", "argv": ["show", receipt_id]})

    wire = json.dumps(resp, ensure_ascii=False)
    assert secret not in wire
    assert "/Users/private" not in wire
    assert "source_ref" not in wire
    assert "export_path" not in wire
    assert artifact_locator not in wire
    assert resp["output"] != result.output
    assert set(resp["receipts"][0]) <= {
        "receipt_id", "status", "subject_id", "subject_kind", "decided_at",
        "content_hash", "scorer_id", "scorer_version", "session_id",
    }
    assert set(resp["receipt"]) <= {
        "receipt_id", "status", "subject_id", "subject_kind", "content_hash",
        "decided_at", "scorer_id", "scorer_version", "session_id", "transaction_id",
        "turn_id", "mission_id", "uncertainty", "claim_count", "evidence_count",
        "artifact_count", "observation_count",
    }


def test_valid_receipt_denial_output_is_redacted(rpc, monkeypatch):
    import hades_cli.receipts as receipts_mod

    secret = "sk-test-ReceiptValidDenialSecret"
    private_path = "/Users/private/receipts/denied.db"
    result = receipts_mod.ReceiptCommandResult(
        receipts_mod.EXIT_OK,
        f"denied {secret} {private_path}",
        {
            "ok": False,
            "action": "show",
            "warning": f"denial warning {secret} {private_path}",
            "receipt": {
                "receipt_id": "rct_" + "c" * 64,
                "status": "failed",
                "uncertainty": [f"uncertainty {secret}"],
            },
        },
    )
    monkeypatch.setattr(receipts_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc("receipt.exec", {"session_id": "sid", "argv": ["show", "rct_" + "c" * 64]})

    wire = json.dumps(resp, ensure_ascii=False)
    assert secret not in wire
    assert private_path not in wire
    assert resp["error"]["code"] == 5040, resp


def test_receipt_native_preserves_colon_subject_and_rfc3339_scalars(
    rpc, monkeypatch
):
    import hades_cli.receipts as receipts_mod

    receipt_id = "rct_" + "a" * 64
    result = receipts_mod.ReceiptCommandResult(
        receipts_mod.EXIT_OK,
        "safe",
        {
            "ok": True,
            "action": "show",
            "receipts": [{
                "receipt_id": receipt_id,
                "status": "verified",
                "subject_id": "s1:t1",
                "subject_kind": "turn",
                "decided_at": "2026-07-10T00:00:00Z",
                "content_hash": "sha256:deadbeef",
                "scorer_id": "scorer",
                "scorer_version": "1.0",
            }],
            "receipt": {
                "receipt_id": receipt_id,
                "status": "verified",
                "subject_id": "s1:t1",
                "subject_kind": "turn",
                "content_hash": "sha256:deadbeef",
                "decided_at": "2026-07-10T00:00:00Z",
                "scorer_id": "scorer",
                "scorer_version": "1.0",
            },
            "observations": [{
                "observation_id": "obs_" + "b" * 64,
                "receipt_id": receipt_id,
                "status": "verified",
                "observed_at": "2026-07-11T09:00:00Z",
            }],
        },
    )
    monkeypatch.setattr(receipts_mod, "run_argv", lambda *_a, **_k: result)

    response = rpc("receipt.exec", {"argv": ["show", receipt_id]})
    assert response["receipts"][0]["subject_id"] == "s1:t1"
    assert response["receipts"][0]["decided_at"] == "2026-07-10T00:00:00Z"
    assert response["receipt"]["subject_id"] == "s1:t1"
    assert response["receipt"]["decided_at"] == "2026-07-10T00:00:00Z"
    assert response["observations"][0]["observed_at"] == "2026-07-11T09:00:00Z"


@pytest.mark.parametrize(
    "path",
    [
        "/Volumes/Untitled/private.txt",
        "/opt/local/secret",
        "/etc/passwd",
        "/root/.ssh/key",
        "/etc",
        "/root",
    ],
)
def test_receipt_native_redacts_generic_posix_paths_recursively(rpc, monkeypatch, path):
    import hades_cli.receipts as receipts_mod

    probe = f"producer free text {path}"
    result = receipts_mod.ReceiptCommandResult(
        receipts_mod.EXIT_OK,
        f"producer output {probe}",
        {
            "ok": True,
            "action": "show",
            "warning": probe,
            "claim_edges": [{
                "claim_id": "claim-1",
                "verdict": "satisfied",
                "required": True,
                "statement": probe,
                "uncertainty": [probe],
            }],
        },
    )
    monkeypatch.setattr(receipts_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc("receipt.exec", {"session_id": "sid", "argv": ["show"]})

    assert path not in json.dumps(resp, ensure_ascii=False)


def test_receipt_claim_edge_locator_ids_are_dropped_but_safe_ids_survive(
    rpc, monkeypatch
):
    import hades_cli.receipts as receipts_mod

    invalid_ids = [
        "artifact://secret/path",
        "file:///tmp/x",
        "artifact:secret",
        "mailto:foo",
        "urn:foo",
        "prefix:/etc",
        "//etc/passwd",
        "/etc/passwd",
        r"C:\\secret",
        "../relative",
        "id..with-dotdot",
        "id with whitespace",
        "control\nid",
        "sha256:not-hex",
    ]
    valid_ids = [
        "550e8400-e29b-41d4-a716-446655440000",
        "evidence_id",
        "artifact.v1",
        "sha256:abc12345",
        "sha256:ABCDEF12",
    ]
    result = receipts_mod.ReceiptCommandResult(
        receipts_mod.EXIT_OK,
        "safe producer output",
        {
            "ok": True,
            "action": "show",
            "claim_edges": [{
                "claim_id": "claim-1",
                "verdict": "satisfied",
                "required": True,
                "evidence_ids": [*invalid_ids, *valid_ids],
                "artifact_ids": [*invalid_ids, *valid_ids],
            }],
        },
    )
    monkeypatch.setattr(receipts_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc("receipt.exec", {"session_id": "sid", "argv": ["show"]})

    edge = resp["claim_edges"][0]
    assert edge["evidence_ids"] == valid_ids
    assert edge["artifact_ids"] == valid_ids
    wire = json.dumps(resp, ensure_ascii=False)
    for invalid_id in invalid_ids:
        assert invalid_id not in wire
    for valid_id in valid_ids:
        assert valid_id in wire


@pytest.mark.parametrize(
    "probe",
    [
        "artifact://secret/path",
        "file:///etc/passwd",
        "//etc/passwd",
        "prefix:/etc/passwd",
        "artifact:secret/path",
        "mailto:foo@example.com",
        "urn:secret:item",
        "https://user:pass@example/x?token=secret",
    ],
)
def test_receipt_native_scrubs_locator_tokens_from_warning_and_claim_text(
    rpc, monkeypatch, probe
):
    import hades_cli.receipts as receipts_mod

    text = f"producer free text {probe}"
    result = receipts_mod.ReceiptCommandResult(
        receipts_mod.EXIT_OK,
        f"producer output {text}",
        {
            "ok": True,
            "action": "show",
            "warning": text,
            "claim_edges": [{
                "claim_id": "claim-1",
                "verdict": "satisfied",
                "required": True,
                "statement": text,
                "uncertainty": [text],
            }],
        },
    )
    monkeypatch.setattr(receipts_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc("receipt.exec", {"session_id": "sid", "argv": ["show"]})

    wire = json.dumps(resp, ensure_ascii=False)
    assert probe not in wire
    for fragment in ("//secret", "secret/path", "secret/item", "/etc", "/root"):
        assert fragment not in wire


def test_receipt_native_preserves_wire_safe_domain_ids_and_drops_unsafe_rows(
    rpc, monkeypatch
):
    import hades_cli.receipts as receipts_mod

    result = receipts_mod.ReceiptCommandResult(
        receipts_mod.EXIT_OK,
        "safe",
        {
            "ok": True,
            "action": "show",
            "receipts": [
                {
                    "receipt_id": "receipt:one",
                    "status": "verified",
                    "subject_id": "project:item-1",
                    "subject_kind": "transaction",
                    "decided_at": "2026-07-10T00:00:00Z",
                    "content_hash": "sha256:deadbeef",
                    "scorer_id": "scorer:one",
                    "scorer_version": "1.0",
                },
                {
                    "receipt_id": "artifact:secret/path",
                    "status": "verified",
                    "subject_id": "project:item-2",
                    "subject_kind": "transaction",
                    "decided_at": "2026-07-10T00:00:00Z",
                    "content_hash": "sha256:deadbeef",
                    "scorer_id": "scorer:one",
                    "scorer_version": "1.0",
                },
            ],
            "receipt": {
                "receipt_id": "receipt:one",
                "status": "verified",
                "subject_id": "project:item-1",
                "subject_kind": "transaction",
                "content_hash": "sha256:deadbeef",
                "decided_at": "2026-07-10T00:00:00Z",
                "scorer_id": "scorer:one",
                "scorer_version": "1.0",
                "transaction_id": "project:item-1",
            },
            "observations": [
                {
                    "observation_id": "observation:one",
                    "receipt_id": "receipt:one",
                    "status": "verified",
                    "observed_at": "2026-07-11T09:00:00Z",
                },
                {
                    "observation_id": "file:/etc",
                    "receipt_id": "receipt:one",
                    "status": "verified",
                    "observed_at": "2026-07-11T09:00:00Z",
                },
            ],
            "claim_edges": [
                {
                    "claim_id": "claim:one",
                    "verdict": "satisfied",
                    "required": True,
                    "evidence_ids": ["evidence-1"],
                    "artifact_ids": ["artifact-1"],
                },
                {
                    "claim_id": "mailto:user",
                    "verdict": "satisfied",
                    "required": True,
                    "evidence_ids": ["evidence-1"],
                    "artifact_ids": ["artifact-1"],
                },
            ],
        },
    )
    monkeypatch.setattr(receipts_mod, "run_argv", lambda *_a, **_k: result)

    response = rpc("receipt.exec", {"argv": ["show", "receipt:one"]})

    assert response["receipts"][0]["receipt_id"] == "receipt:one"
    assert response["receipts"][0]["subject_id"] == "project:item-1"
    assert response["receipt"]["transaction_id"] == "project:item-1"
    assert response["observations"][0]["observation_id"] == "observation:one"
    assert response["claim_edges"][0]["claim_id"] == "claim:one"
    assert response["claim_edges"][0]["evidence_ids"] == ["evidence-1"]
    assert response["claim_edges"][0]["artifact_ids"] == ["artifact-1"]
    wire = json.dumps(response, ensure_ascii=False)
    for unsafe in ("artifact:secret/path", "file:/etc", "mailto:user"):
        assert unsafe not in wire

    required = {
        "receipt_id", "subject_id", "subject_kind", "decided_at", "content_hash",
        "scorer_id", "scorer_version", "observation_id", "observed_at", "claim_id",
        "verdict",
    }

    def assert_required_values(value):
        if isinstance(value, dict):
            for key, nested in value.items():
                if key in required:
                    assert nested is not None, (key, value)
                assert_required_values(nested)
        elif isinstance(value, list):
            for nested in value:
                assert_required_values(nested)

    assert_required_values(response)


def test_receipt_native_rejects_path_bearing_required_domain_ids(rpc, monkeypatch):
    import hades_cli.receipts as receipts_mod

    safe_ids = ["receipt:one", "project:item-1", "scorer:one", "团队:项目"]
    invalid_ids = [
        "team:email/secret",
        "foo:bar/baz",
        r"team:email\secret",
        "safe/../bad",
        "artifact:secret/path",
        "",
        "control\nid",
        "x" * 257,
    ]

    def summary(receipt_id, subject_id="project:item-1", scorer_id="scorer:one"):
        return {
            "receipt_id": receipt_id,
            "status": "verified",
            "subject_id": subject_id,
            "subject_kind": "transaction",
            "decided_at": "2026-07-10T00:00:00Z",
            "content_hash": "sha256:deadbeef",
            "scorer_id": scorer_id,
            "scorer_version": "1.0",
        }

    summaries = [summary(safe_ids[0], safe_ids[1], safe_ids[2])]
    summaries.extend(summary(invalid_id) for invalid_id in invalid_ids)
    summaries.extend([
        summary(safe_ids[0], invalid_ids[0]),
        summary(safe_ids[0], safe_ids[1], invalid_ids[1]),
    ])
    result = receipts_mod.ReceiptCommandResult(
        receipts_mod.EXIT_OK,
        "safe",
        {
            "ok": True,
            "action": "show",
            "receipts": summaries,
            "receipt": summary(safe_ids[0], invalid_ids[2], safe_ids[2]),
            "observations": [
                {
                    "observation_id": safe_ids[0],
                    "receipt_id": safe_ids[0],
                    "status": "verified",
                    "observed_at": "2026-07-11T09:00:00Z",
                },
                {
                    "observation_id": invalid_ids[3],
                    "receipt_id": safe_ids[0],
                    "status": "verified",
                    "observed_at": "2026-07-11T09:00:00Z",
                },
            ],
            "claim_edges": [
                {
                    "claim_id": safe_ids[1],
                    "verdict": "satisfied",
                    "required": True,
                    "evidence_ids": ["evidence-1"],
                    "artifact_ids": ["artifact-1"],
                },
                {
                    "claim_id": invalid_ids[4],
                    "verdict": "satisfied",
                    "required": True,
                    "evidence_ids": ["evidence-1"],
                    "artifact_ids": ["artifact-1"],
                },
            ],
        },
    )
    monkeypatch.setattr(receipts_mod, "run_argv", lambda *_a, **_k: result)

    response = rpc("receipt.exec", {"argv": ["show", safe_ids[0]]})

    assert response["receipts"] == [summaries[0]]
    assert "receipt" not in response
    assert [row["observation_id"] for row in response["observations"]] == [safe_ids[0]]
    assert [row["claim_id"] for row in response["claim_edges"]] == [safe_ids[1]]
    wire = json.dumps(response, ensure_ascii=False)
    for invalid_id in invalid_ids:
        if invalid_id:
            assert invalid_id not in wire
