"""Tests for the shared top-level and classic CLI receipt viewer (Task 8).

Covers ``hades_cli/receipts.py`` against a real profile-local
``SessionDB``, real receipts, real observations, and the real
exporter/retention/signing services:

- ``show`` distinguishes the immutable original decision from the
  latest recheck observation and surfaces drift truthfully.
- ``export`` defaults to the public redaction (no raw locators, no
  profile-home prefixes) and ``prune`` refuses anything but the exact
  current retention-plan hash.
- ``list``/``claims``/``recheck``/``verify-signature``/
  ``retention-plan`` render structured records; ``--json`` output is
  machine-parseable and carries claim→evidence→artifact edges.
- ``completed_unverified`` is never rendered as success and
  ``unknown_effect`` is never rendered as failure or retry-safe.
- argv count/byte bounds are enforced without echoing oversized (or
  secret-bearing) arguments, unknown IDs fail without a traceback, and
  relative output paths cannot escape the working directory.
- The classic ``/receipt`` path and the top-level ``hades receipt``
  parser produce identical output from the same shared service.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac as hmac_module
import json
import os
from pathlib import Path

import pytest

from hades_state import SessionDB
from agent.receipt_artifacts import ArtifactCatalog
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
from hades_cli.receipts import (
    ReceiptCommandResult,
    build_parser,
    receipt_command,
    run_argv,
    run_slash,
)

RECENT_DECIDED_AT = "2026-07-10T00:00:00Z"
OLD_DECIDED_AT = "2024-01-01T00:00:00Z"

_HMAC_KEY = b"cli-test-signing-key"


# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture()
def home() -> Path:
    """The per-test profile home the CLI resolves via ``get_hades_home``."""
    return Path(os.environ["HADES_HOME"])


@pytest.fixture()
def db(home):
    session_db = SessionDB(db_path=home / "state.db")
    yield session_db
    session_db.close()


@pytest.fixture()
def store(db):
    return ReceiptStore(db)


@pytest.fixture()
def workdir(tmp_path, monkeypatch) -> Path:
    """An isolated working directory for relative ``--output`` paths."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


class _CliHarness:
    def __init__(self, home: Path, workdir: Path) -> None:
        self.home = home
        self.workdir = workdir

    def run(self, argv, *, output: str = "text") -> ReceiptCommandResult:
        return run_argv(argv, output=output)


@pytest.fixture()
def cli(home, workdir, db) -> _CliHarness:
    return _CliHarness(home, workdir)


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
    artifacts=(),
    uncertainty: tuple[str, ...] = (),
):
    evidence = build_evidence_digest(
        evidence_kind="verification_check",
        source_ref=f"verification_evidence.db:check:{source_id}",
        producer_id="hades.verification",
        observed_at=decided_at,
        summary="pytest ran after final edit",
        payload_hash=canonical_content_hash({"check": "pytest", "id": source_id}),
        artifact_ids=tuple(a.artifact_id for a in artifacts),
    )
    claim = build_claim(
        statement=statement,
        evidence_ids=(evidence.evidence_id,),
        artifact_ids=tuple(a.artifact_id for a in artifacts),
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
        artifacts=tuple(artifacts),
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
def receipt(store):
    """The plan's baseline seeded receipt fixture."""
    return store.insert(_make_receipt())


@pytest.fixture()
def seeded_receipt(receipt):
    return receipt


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


class _HmacSigner:
    provider_id = "test-hmac"

    def sign(self, content_hash: str):
        from agent.receipt_security import SignatureMaterial

        digest = hmac_module.new(
            _HMAC_KEY, content_hash.encode("utf-8"), hashlib.sha256
        ).digest()
        return SignatureMaterial(
            key_id="k1",
            algorithm="hmac-sha256",
            signature_b64=base64.b64encode(digest).decode("ascii"),
        )

    def verify(self, content_hash: str, material) -> bool:
        expected = self.sign(content_hash).signature_b64
        return hmac_module.compare_digest(expected, material.signature_b64)


@pytest.fixture()
def hmac_signer_registered():
    from agent.receipt_security import (
        register_receipt_signer,
        unregister_receipt_signer,
    )

    register_receipt_signer(
        "test-hmac", lambda config: _HmacSigner(), lambda config: True
    )
    yield
    unregister_receipt_signer("test-hmac")


# =========================================================================
# Plan-specified RED tests
# =========================================================================


def test_show_distinguishes_original_from_latest_observation(cli, receipt_with_drift):
    result = cli.run(["show", receipt_with_drift.receipt_id])
    assert result.exit_code == 0
    assert "Original: verified" in result.stdout
    assert "Latest recheck: failed" in result.stdout
    assert "Artifact hash changed" in result.stdout


def test_export_defaults_public_and_prune_requires_exact_plan(cli, receipt):
    exported = cli.run(["export", receipt.receipt_id, "--output", "receipt.json"])
    assert exported.exit_code == 0
    assert cli.home.as_posix() not in Path("receipt.json").read_text("utf-8")
    refused = cli.run(["prune", "--confirm-plan", "wrong"])
    assert refused.exit_code == 2


# =========================================================================
# list
# =========================================================================


def test_list_renders_seeded_receipt_and_truthful_status(cli, receipt):
    result = cli.run(["list"])
    assert result.exit_code == 0
    assert receipt.receipt_id in result.stdout
    assert "completed_unverified" in result.stdout
    # completed_unverified must never be rendered as success.
    assert "success" not in result.stdout.lower()


def test_list_filters_by_status_subject_and_limit(cli, store):
    store.insert(_make_receipt(source_id="s1:t1", turn_id="t1"))
    store.insert(
        _make_receipt(
            source_id="s1:t2", turn_id="t2", status="failed", verdict="unsatisfied"
        )
    )
    failed = cli.run(["list", "--status", "failed", "--json"])
    assert failed.exit_code == 0
    payload = json.loads(failed.stdout)
    assert len(payload["receipts"]) == 1
    assert payload["receipts"][0]["status"] == "failed"

    limited = cli.run(["list", "--subject", "turn", "--limit", "1", "--json"])
    assert limited.exit_code == 0
    assert len(json.loads(limited.stdout)["receipts"]) == 1

    none = cli.run(["list", "--subject", "mission", "--json"])
    assert none.exit_code == 0
    assert json.loads(none.stdout)["receipts"] == []


def test_list_reads_receipts_with_capture_disabled(cli, home, receipt):
    (home / "config.yaml").write_text("receipts:\n  mode: off\n", encoding="utf-8")
    result = cli.run(["list"])
    assert result.exit_code == 0
    assert receipt.receipt_id in result.stdout
    # Disabled capture is stated truthfully without hiding stored receipts.
    assert "off" in result.stdout


# =========================================================================
# show / claims rendering
# =========================================================================


def test_show_json_exposes_receipt_and_claim_edges(cli, seeded_receipt):
    result = cli.run(["show", seeded_receipt.receipt_id, "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["receipt"]["content_hash"] == seeded_receipt.content_hash
    assert payload["claim_edges"][0]["evidence_ids"]
    assert result.payload["receipt"]["receipt_id"] == seeded_receipt.receipt_id


def test_show_renders_outcome_scorer_uncertainty_and_copyable_commands(
    cli, seeded_receipt
):
    result = cli.run(["show", seeded_receipt.receipt_id])
    assert result.exit_code == 0
    out = result.stdout
    assert "add marker to README" in out
    assert "no force push" in out
    assert seeded_receipt.scorer_id in out
    assert seeded_receipt.decided_at in out
    # Copyable follow-up commands.
    assert f"receipt recheck {seeded_receipt.receipt_id}" in out
    assert f"receipt export {seeded_receipt.receipt_id}" in out
    # completed_unverified is never success.
    assert "success" not in out.lower()


def test_show_unknown_effect_warns_do_not_retry(cli, store):
    stored = store.insert(
        _make_receipt(
            source_id="s1:t3",
            turn_id="t3",
            status="unknown_effect",
            verdict="unknown",
            statement="the notification was delivered",
        )
    )
    result = cli.run(["show", stored.receipt_id])
    assert result.exit_code == 0
    assert "unknown_effect" in result.stdout
    assert "do not retry" in result.stdout.lower()
    # unknown_effect is neither a failure nor retry-safe.
    assert "failed" not in result.stdout.lower()
    assert "retry-safe" not in result.stdout.lower()


def test_show_observation_all_lists_the_chain(cli, receipt_with_drift):
    result = cli.run(["show", receipt_with_drift.receipt_id, "--observation", "all"])
    assert result.exit_code == 0
    assert "Original: verified" in result.stdout
    assert "failed" in result.stdout


def test_claims_renders_claim_evidence_artifact_edges(cli, store, db):
    catalog = ArtifactCatalog(db)
    digest = catalog.register_bytes(
        b"final deliverable bytes",
        source_kind="turn",
        source_ref="s1:t4:artifact",
        display_name="deliverable.txt",
    )
    stored = store.insert(
        _make_receipt(source_id="s1:t4", turn_id="t4", artifacts=(digest,))
    )
    claim = stored.claims[0]
    result = cli.run(["claims", stored.receipt_id])
    assert result.exit_code == 0
    assert claim.claim_id in result.stdout
    assert claim.evidence_ids[0] in result.stdout
    assert digest.artifact_id in result.stdout

    as_json = cli.run(["claims", stored.receipt_id, "--json"])
    payload = json.loads(as_json.stdout)
    edge = payload["claim_edges"][0]
    assert edge["evidence_ids"] == list(claim.evidence_ids)
    assert edge["artifact_ids"] == [digest.artifact_id]


# =========================================================================
# recheck
# =========================================================================


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


def test_recheck_appends_observation_and_never_rewrites_original(cli, db, store):
    from agent.receipt_ingest import build_receipt_issuer

    db.record_turn_outcome(_turn_record())
    original = build_receipt_issuer(db).issue(ReceiptSourceKey("turn", "s1:t1"))
    result = cli.run(["recheck", original.receipt_id])
    assert result.exit_code == 0
    chain = store.observations(original.receipt_id)
    assert len(chain) == 1
    assert chain[0].status in RECEIPT_STATUSES
    assert store.get(original.receipt_id) == original
    assert original.receipt_id in result.stdout


def test_recheck_unknown_receipt_fails_without_traceback(cli):
    result = cli.run(["recheck", "rct_" + "0" * 64])
    assert result.exit_code == 2
    assert "Traceback" not in result.stdout


# =========================================================================
# export safety
# =========================================================================


def test_export_relative_output_cannot_escape_cwd(cli, seeded_receipt, tmp_path):
    result = cli.run(
        ["export", seeded_receipt.receipt_id, "--output", "../escape.json"]
    )
    assert result.exit_code == 2
    assert not (tmp_path / "escape.json").exists()


def test_export_sign_without_provider_stays_truthfully_unsigned(cli, seeded_receipt):
    result = cli.run(
        ["export", seeded_receipt.receipt_id, "--output", "signed.json", "--sign"]
    )
    assert result.exit_code == 0
    assert "unsigned" in result.stdout.lower()
    data = json.loads(Path("signed.json").read_text("utf-8"))
    assert data["attestations"] == []
    assert data["receipt"]["status"] == "completed_unverified"


# =========================================================================
# verify-signature
# =========================================================================


def test_verify_signature_is_provenance_only(
    cli, home, store, seeded_receipt, hmac_signer_registered
):
    (home / "config.yaml").write_text(
        "receipts:\n  signing:\n    provider: test-hmac\n    required: false\n",
        encoding="utf-8",
    )
    signed = cli.run(
        ["export", seeded_receipt.receipt_id, "--output", "signed.json", "--sign"]
    )
    assert signed.exit_code == 0
    result = cli.run(["verify-signature", seeded_receipt.receipt_id])
    assert result.exit_code == 0
    assert "provenance only" in result.stdout.lower()

    as_json = cli.run(["verify-signature", seeded_receipt.receipt_id, "--json"])
    payload = json.loads(as_json.stdout)
    assert payload["attestations"][0]["valid"] is True
    # A valid signature never changes the stored truth status.
    assert store.get(seeded_receipt.receipt_id).status == "completed_unverified"


# =========================================================================
# retention-plan / prune
# =========================================================================


def test_retention_plan_and_prune_roundtrip(cli, store):
    old = store.insert(
        _make_receipt(source_id="s9:t9", turn_id="t9", decided_at=OLD_DECIDED_AT)
    )
    planned = cli.run(["retention-plan", "--json"])
    assert planned.exit_code == 0
    payload = json.loads(planned.stdout)
    plan_hash = payload["retention_plan_hash"]
    assert plan_hash.startswith("sha256:")
    assert old.receipt_id in payload["plan"]["receipt_ids"]

    pruned = cli.run(["prune", "--confirm-plan", plan_hash, "--json"])
    assert pruned.exit_code == 0
    assert store.get(old.receipt_id) is None
    tombstones = store.list_tombstones()
    assert any(t.receipt_id == old.receipt_id for t in tombstones)

    # The copyable prune command appears in the text plan rendering too.
    replan = cli.run(["retention-plan"])
    assert replan.exit_code == 0
    assert "prune --confirm-plan" in replan.stdout


# =========================================================================
# argv bounds and leakage
# =========================================================================


def test_too_many_arguments_are_refused(cli):
    result = cli.run(["list"] + ["x"] * 65)
    assert result.exit_code == 2
    assert "Traceback" not in result.stdout


def test_oversized_argument_is_refused_without_echo(cli):
    secret = "sk-live-" + "x" * 70_000
    result = cli.run(["show", secret])
    assert result.exit_code == 2
    assert secret not in result.stdout
    assert "sk-live-" not in result.stdout


def test_unknown_receipt_id_fails_without_traceback(cli):
    result = cli.run(["show", "rct_" + "f" * 64])
    assert result.exit_code == 2
    assert "Traceback" not in result.stdout


# =========================================================================
# parser parity: top-level, classic, and help
# =========================================================================


def test_bare_and_help_invocations_return_help(cli):
    bare = cli.run([])
    assert bare.exit_code == 0
    for verb in (
        "list",
        "show",
        "claims",
        "recheck",
        "export",
        "verify-signature",
        "retention-plan",
        "prune",
    ):
        assert verb in bare.stdout
    helped = cli.run(["--help"])
    assert helped.exit_code == 0


def test_top_level_and_classic_output_parity(cli, seeded_receipt, capsys):
    root = argparse.ArgumentParser(prog="hades")
    sub = root.add_subparsers(dest="command")
    build_parser(sub)
    args = root.parse_args(["receipt", "show", seeded_receipt.receipt_id])
    code = receipt_command(args)
    top_level = capsys.readouterr().out
    classic = run_slash(f"show {seeded_receipt.receipt_id}")
    assert code == 0
    assert top_level.strip() == classic.strip()


def test_receipts_alias_parses_at_top_level(cli, seeded_receipt, capsys):
    root = argparse.ArgumentParser(prog="hades")
    sub = root.add_subparsers(dest="command")
    build_parser(sub)
    args = root.parse_args(["receipts", "list"])
    code = receipt_command(args)
    out = capsys.readouterr().out
    assert code == 0
    assert seeded_receipt.receipt_id in out
