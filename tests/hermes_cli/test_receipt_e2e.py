"""Real-path E2E proof for verified outcome receipts (Task 11).

Proves the 90-day contract end to end over a temporary profile home with
real ``state.db``/``verification_evidence.db`` SQLite files, real
mission/workflow fixture records, real operation-journal rows, real
files and hashes, the real CLI service, and genuine subprocess restarts:

- crash/replay recovery is idempotent across six injected fault points,
  each exercised by a real subprocess that hard-exits at the point;
- every one of the exact 50 preregistered false-success missions is run
  in a fresh process, lands on its preregistered terminal status, never
  scores ``verified``, keeps every claim traceable to existing evidence,
  and is independently recheckable from another new process;
- replay, forgery, staleness, symlink/locator swaps, ambiguity,
  redaction canaries, retention holds, and cross-profile access all fail
  safely and truthfully.

Only the external signing boundary uses a fake (an HMAC signer) and the
partial-delivery ambiguity comes from the manifest's fixture-backed
platform boundary; everything else is the real code path.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac as hmac_module
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.operation_journal import OperationJournal
from agent.receipt_artifacts import ArtifactCatalog
from agent.receipt_hashing import canonical_content_hash
from agent.receipt_ingest import (
    SnapshotConflictError,
    build_receipt_issuer,
)
from agent.receipt_models import (
    VerifiedReceiptDecision,
    build_claim,
    build_evidence_digest,
    build_receipt,
    build_requested_outcome,
)
from agent.receipt_scoring import (
    ReceiptScoringService,
    ScorerEvaluation,
    ScorerIndependenceError,
    ScorerRegistry,
)
from agent.receipt_security import (
    ReceiptExporter,
    ReceiptRetentionService,
    ReceiptSigningService,
    RetentionHold,
    RetentionHoldError,
    SignatureMaterial,
    verify_export_hashes,
)
from agent.receipt_store import ReceiptStore
from agent.receipts import ReceiptQuery, ReceiptSourceKey
from agent.turn_ledger import TurnOutcomeRecord
from agent.verification_evidence import (
    mark_workspace_edited,
    record_terminal_result,
)
from benchmarks.receipts.cases import load_receipt_cases
from benchmarks.receipts.runner import (
    recheck_case,
    run_single_case,
)
from hades_state import SessionDB

_REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = _REPO_ROOT / "benchmarks" / "receipts" / "manifest.yaml"

_, RECEIPT_CASES = load_receipt_cases(MANIFEST)

_T0 = 1752660000  # fixed epoch for durable fixture timestamps

FAULT_POINTS = (
    "after_source_snapshot",
    "after_receipt_insert",
    "before_subject_projection",
    "after_subject_projection",
    "during_artifact_hash",
    "after_observation_insert",
)

# Exit code the injected FaultHook uses for a simulated hard crash.
_FAULT_EXIT_CODE = 23

# Fixture-backed vertical-slice missions table (schema copied from the
# preregistered plan document — this clone has no missions implementation).
_MISSIONS_DDL = (
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
    )""",
    """CREATE TABLE IF NOT EXISTS mission_execution_links (
        mission_id TEXT NOT NULL,
        execution_id TEXT NOT NULL,
        relation TEXT NOT NULL DEFAULT 'primary',
        linked_at INTEGER NOT NULL,
        PRIMARY KEY (mission_id, execution_id)
    )""",
)

# Exact provisional v1 vertical-slice receipt tables (migration input).
_V1_RECEIPTS_DDL = """
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    status TEXT NOT NULL,
    objective TEXT NOT NULL,
    constraints_json TEXT NOT NULL,
    execution_ids_json TEXT NOT NULL,
    transaction_ids_json TEXT NOT NULL,
    before_after_json TEXT NOT NULL,
    claims_json TEXT NOT NULL,
    verifier_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    artifacts_json TEXT NOT NULL,
    uncertainty_json TEXT NOT NULL,
    freshness_json TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    signature_json TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS receipt_observations (
    observation_id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL REFERENCES receipts(receipt_id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL
);
"""


def _symlinks_supported() -> bool:
    import tempfile

    with tempfile.TemporaryDirectory() as probe:
        target = Path(probe) / "target.txt"
        target.write_text("x")
        try:
            (Path(probe) / "link.txt").symlink_to(target)
        except (OSError, NotImplementedError):
            return False
    return True


requires_symlinks = pytest.mark.skipif(
    not _symlinks_supported(),
    reason="platform cannot create symlinks without extra privilege",
)


# =========================================================================
# Subprocess child scripts (real process restarts, real hard exits)
# =========================================================================

# Drives the exact mission issue -> project -> recheck sequence through
# the public receipt services with an injected FaultHook(point, context)
# that hard-exits the process at the requested point.
_FAULT_CHILD_SCRIPT = textwrap.dedent(
    """
    import os
    import sys
    from pathlib import Path

    fault_point = sys.argv[1]
    home = Path(sys.argv[2])
    workflows = Path(sys.argv[3])
    artifact_root = Path(sys.argv[4])

    from hades_state import SessionDB
    from agent.receipt_artifacts import ArtifactCatalog
    from agent.receipt_ingest import (
        ReceiptIngestor,
        ReceiptIssuer,
        ReceiptSourceResolver,
    )
    from agent.receipt_scoring import build_default_scoring_service
    from agent.receipt_store import ReceiptStore
    from agent.receipts import ReceiptSourceKey

    def fault_hook(point, context=None):
        if point == fault_point:
            os._exit(23)

    class FaultingCatalog(ArtifactCatalog):
        def recheck(self, artifact_id, *, allowed_roots=()):
            fault_hook("during_artifact_hash", {"artifact_id": artifact_id})
            return super().recheck(artifact_id, allowed_roots=allowed_roots)

    db = SessionDB(db_path=home / "state.db")
    catalog = FaultingCatalog(db)
    store = ReceiptStore(db)
    scoring = build_default_scoring_service(
        catalog=catalog, allowed_roots=(artifact_root,)
    )
    sources = ReceiptSourceResolver(
        db,
        workflows_db_path=workflows,
        profile="default",
        catalog=catalog,
        allowed_roots=(artifact_root,),
    )
    issuer = ReceiptIssuer(
        store, scoring=scoring, sources=sources, workflows_db_path=workflows
    )
    source = ReceiptSourceKey("mission", "m1")

    snapshot = sources.for_key(source).snapshot()
    fault_hook("after_source_snapshot", {"snapshot": snapshot.content_hash})
    receipt = ReceiptIngestor(
        store, decide=scoring.decide, workflows_db_path=workflows
    ).issue(snapshot)
    fault_hook("after_receipt_insert", {"receipt_id": receipt.receipt_id})
    fault_hook("before_subject_projection", {"receipt_id": receipt.receipt_id})
    issuer.recover_projection(source)
    fault_hook("after_subject_projection", {"receipt_id": receipt.receipt_id})
    observation = issuer.recheck(receipt.receipt_id)
    fault_hook(
        "after_observation_insert",
        {"observation_id": observation.observation_id},
    )
    # The harness always names one of the points above; reaching here
    # means the injected fault never fired.
    os._exit(9)
    """
)


def _run_subprocess(args, *, home: Path, timeout: int = 240):
    env = os.environ.copy()
    env["HADES_HOME"] = str(home)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return subprocess.run(
        [sys.executable, *args],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# =========================================================================
# The E2E harness
# =========================================================================


class ReceiptE2EHarness:
    """Temporary-profile harness over real stores, files, and processes."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = Path(tmp_path)
        self._case_homes: dict[str, Path] = {}
        # Fault-matrix mission fixture home.
        self.home = self.tmp_path / "fault-home"
        self.home.mkdir()
        self.workflows_db = self.home / "workflows.db"
        self.artifact_root = self.home / "deliverables"
        self.artifact_root.mkdir()
        self.artifact_path = self.artifact_root / "notes.txt"
        self.artifact_path.write_text("published release notes\n")
        self._seed_mission_fixture()

    # ── Fault-matrix fixture ──

    def _seed_mission_fixture(self) -> None:
        db = SessionDB(db_path=self.home / "state.db")
        try:
            digest = ArtifactCatalog(db).register_path(
                self.artifact_path,
                source_kind="mission",
                source_ref="m1:artifact",
                allowed_roots=(self.artifact_root,),
            )
        finally:
            db.close()
        conn = sqlite3.connect(self.workflows_db)
        try:
            for statement in _MISSIONS_DDL:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO missions (mission_id, profile, objective, "
                "constraints_json, authority_json, evidence_json, status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "m1",
                    "default",
                    "publish the release notes page",
                    json.dumps(["no purchases"]),
                    json.dumps({"scopes": ["web"]}),
                    json.dumps(
                        {
                            "artifact_ids": [digest.artifact_id],
                            "before": {"page": "absent"},
                            "after": {"page": "published"},
                        }
                    ),
                    "completed",
                    _T0,
                    _T0 + 60,
                ),
            )
            conn.execute(
                "INSERT INTO mission_execution_links (mission_id, "
                "execution_id, relation, linked_at) "
                "VALUES ('m1', 'exec-1', 'primary', ?)",
                (_T0,),
            )
            conn.commit()
        finally:
            conn.close()

    def issue_with_subprocess_exit(self, fault_point: str) -> None:
        """Run the issue/project/recheck sequence in a real subprocess
        that hard-exits at *fault_point* via the injected FaultHook."""
        result = _run_subprocess(
            [
                "-c",
                _FAULT_CHILD_SCRIPT,
                fault_point,
                str(self.home),
                str(self.workflows_db),
                str(self.artifact_root),
            ],
            home=self.home,
        )
        assert result.returncode == _FAULT_EXIT_CODE, (
            f"fault subprocess for {fault_point!r} exited "
            f"{result.returncode}, expected {_FAULT_EXIT_CODE}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def reopen_all_stores_and_reconcile(self) -> SimpleNamespace:
        """Fresh object graph over the same durable files; reconcile."""
        db = SessionDB(db_path=self.home / "state.db")
        try:
            # Repair any interrupted artifact capture: registration is
            # content-addressed and idempotent.
            ArtifactCatalog(db).register_path(
                self.artifact_path,
                source_kind="mission",
                source_ref="m1:artifact",
                allowed_roots=(self.artifact_root,),
            )
            issuer = build_receipt_issuer(
                db,
                workflows_db_path=self.workflows_db,
                profile="default",
                allowed_roots=(self.artifact_root,),
            )
            source = ReceiptSourceKey("mission", "m1")
            receipt = issuer.issue(source)
            receipt_count = len(issuer.store.list(ReceiptQuery()))
            recomputed_hash = build_receipt(
                source=receipt.source,
                subject_kind=receipt.subject_kind,
                subject_id=receipt.subject_id,
                session_id=receipt.session_id,
                turn_id=receipt.turn_id,
                mission_id=receipt.mission_id,
                transaction_id=receipt.transaction_id,
                requested_outcome=receipt.requested_outcome,
                status=receipt.status,
                claims=receipt.claims,
                evidence=receipt.evidence,
                artifacts=receipt.artifacts,
                uncertainty=receipt.uncertainty,
                scorer_id=receipt.scorer_id,
                scorer_version=receipt.scorer_version,
                decided_at=receipt.decided_at,
            ).content_hash
            fresh_snapshot = issuer.sources.for_key(source).snapshot()
            independent = isinstance(
                issuer.scoring.decide(fresh_snapshot), VerifiedReceiptDecision
            )
        finally:
            db.close()
        conn = sqlite3.connect(self.workflows_db)
        try:
            row = conn.execute(
                "SELECT receipt_id FROM missions WHERE mission_id = 'm1'"
            ).fetchone()
        finally:
            conn.close()
        return SimpleNamespace(
            receipt_count=receipt_count,
            subject=SimpleNamespace(receipt_id=row[0] if row else None),
            receipt=receipt,
            recomputed_hash=recomputed_hash,
            independent_evidence_passed=independent,
        )

    # ── Seeded 50-mission corpus ──

    def run_case_in_fresh_process(self, case) -> SimpleNamespace:
        case_home = self.tmp_path / "cases" / case.case_id
        case_home.mkdir(parents=True)
        result_json = case_home / "issue-result.json"
        result = _run_subprocess(
            [
                "-m",
                "benchmarks.receipts.runner",
                "--manifest",
                str(MANIFEST),
                "--run-case",
                case.case_id,
                "--case-home",
                str(case_home),
                "--result-json",
                str(result_json),
            ],
            home=case_home,
        )
        assert result.returncode == 0, (
            f"case subprocess for {case.case_id} failed "
            f"({result.returncode})\nstdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
        payload = json.loads(result_json.read_text(encoding="utf-8"))
        self._case_homes[payload["receipt_id"]] = case_home
        return SimpleNamespace(
            receipt_id=payload["receipt_id"],
            status=payload["status"],
            claim_count=payload["claim_count"],
            traceable_claim_count=payload["traceable_claim_count"],
        )

    def recheck_in_new_process(self, receipt_id: str) -> SimpleNamespace:
        case_home = self._case_homes[receipt_id]
        result_json = case_home / "recheck-result.json"
        result = _run_subprocess(
            [
                "-m",
                "benchmarks.receipts.runner",
                "--recheck",
                "--case-home",
                str(case_home),
                "--result-json",
                str(result_json),
            ],
            home=case_home,
        )
        assert result.returncode == 0, (
            f"recheck subprocess failed ({result.returncode})\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        payload = json.loads(result_json.read_text(encoding="utf-8"))
        return SimpleNamespace(
            receipt_id=payload["receipt_id"],
            observation_id=payload["observation_id"],
            status=payload["status"],
        )


@pytest.fixture()
def receipt_e2e(tmp_path):
    return ReceiptE2EHarness(tmp_path)


# =========================================================================
# Plan-specified matrix: crash recovery and the exact 50 seeded missions
# =========================================================================


@pytest.mark.parametrize("fault_point", [
    "after_source_snapshot", "after_receipt_insert", "before_subject_projection",
    "after_subject_projection", "during_artifact_hash", "after_observation_insert",
])
def test_receipt_recovery_is_idempotent(receipt_e2e, fault_point):
    receipt_e2e.issue_with_subprocess_exit(fault_point)
    final = receipt_e2e.reopen_all_stores_and_reconcile()
    assert final.receipt_count == 1
    assert final.subject.receipt_id == final.receipt.receipt_id
    assert final.receipt.content_hash == final.recomputed_hash
    assert final.receipt.status != "verified" or final.independent_evidence_passed


@pytest.mark.parametrize("case", RECEIPT_CASES, ids=lambda c: c.case_id)
def test_each_seeded_mission_is_traceable_and_recheckable(receipt_e2e, case):
    result = receipt_e2e.run_case_in_fresh_process(case)
    assert result.status == case.expected_status
    assert result.status != "verified"
    assert result.claim_count == result.traceable_claim_count
    assert receipt_e2e.recheck_in_new_process(result.receipt_id).receipt_id == result.receipt_id


# =========================================================================
# Replay: identical, conflicting, observation, and attestation replays
# =========================================================================


def _turn_record(session_id="s1", turn_id="t1", **overrides) -> TurnOutcomeRecord:
    fields = dict(
        session_id=session_id,
        turn_id=turn_id,
        created_at=float(_T0),
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
        model="e2e-model",
    )
    fields.update(overrides)
    return TurnOutcomeRecord(**fields)


@pytest.fixture()
def db():
    home = Path(os.environ["HADES_HOME"])
    session_db = SessionDB(db_path=home / "state.db")
    yield session_db
    session_db.close()


class _HmacSigner:
    """The one faked boundary: an external signing provider."""

    provider_id = "e2e-hmac"
    _key = b"e2e-signing-key"

    def sign(self, content_hash: str) -> SignatureMaterial:
        digest = hmac_module.new(
            self._key, content_hash.encode("utf-8"), hashlib.sha256
        ).digest()
        return SignatureMaterial(
            key_id="k1",
            algorithm="hmac-sha256",
            signature_b64=base64.b64encode(digest).decode("ascii"),
        )

    def verify(self, content_hash: str, material: SignatureMaterial) -> bool:
        expected = self.sign(content_hash).signature_b64
        return hmac_module.compare_digest(expected, material.signature_b64)


def test_identical_replay_returns_receipt_and_conflicting_replay_fails(db):
    db.record_turn_outcome(_turn_record())
    issuer = build_receipt_issuer(db)
    source = ReceiptSourceKey("turn", "s1:t1")
    first = issuer.issue(source)
    assert issuer.issue(source) == first
    assert len(issuer.store.list(ReceiptQuery())) == 1

    # Change the durable source content: a later operation lands in the
    # same turn. Reusing the terminal source identity is a conflict.
    journal = OperationJournal(db)
    journal.create(
        operation_id="op-conflict",
        kind="effect",
        session_id="s1",
        turn_id="t1",
        tool_call_id="call-conflict",
    )
    with pytest.raises(SnapshotConflictError):
        issuer.issue(source)
    # The stored receipt is untouched by the conflicting replay.
    assert issuer.store.get(first.receipt_id) == first


def test_observation_replay_and_attestation_replay_cannot_retarget(db):
    db.record_turn_outcome(_turn_record())
    db.record_turn_outcome(_turn_record(turn_id="t2"))
    issuer = build_receipt_issuer(db)
    store = issuer.store
    receipt = issuer.issue(ReceiptSourceKey("turn", "s1:t1"))
    other = issuer.issue(ReceiptSourceKey("turn", "s1:t2"))

    observation = issuer.recheck(receipt.receipt_id)
    # Identical observation replay (e.g. a crash-retry) returns the same
    # stored observation and never duplicates the chain.
    assert store.append_observation(observation) == observation
    assert len(store.observations(receipt.receipt_id)) == 1

    signing = ReceiptSigningService(
        store, provider_id="e2e-hmac", signer=_HmacSigner()
    )
    attestation = signing.sign(receipt)
    assert attestation is not None
    assert signing.verify(attestation).valid

    # Replaying the attestation against another receipt's hash fails:
    # a signature is bound to exactly one content hash.
    replayed = dataclasses.replace(
        attestation,
        target_id=other.receipt_id,
        target_content_hash=other.content_hash,
    )
    assert not signing.verify(replayed).valid
    # And signing never changed either truth status.
    assert store.get(receipt.receipt_id).status == receipt.status
    assert store.get(other.receipt_id).status == other.status


# =========================================================================
# Artifact swaps: bytes, same-size bytes, mtime, locator, symlink
# =========================================================================


@pytest.fixture()
def swap_workspace(db, tmp_path):
    root = tmp_path / "swap-root"
    root.mkdir()
    path = root / "report.txt"
    path.write_bytes(b"original artifact bytes")
    catalog = ArtifactCatalog(db)
    digest = catalog.register_path(
        path,
        source_kind="execute_code",
        source_ref="s1:t1:call-1",
        allowed_roots=(root,),
    )
    return SimpleNamespace(root=root, path=path, catalog=catalog, digest=digest)


def _single_recheck(ws):
    results = ws.catalog.recheck(ws.digest.artifact_id, allowed_roots=(ws.root,))
    assert len(results) == 1
    return results[0]


def test_artifact_byte_swap_is_detected(swap_workspace):
    swap_workspace.path.write_bytes(b"tampered bytes, different size")
    assert _single_recheck(swap_workspace).status == "changed"


def test_artifact_same_size_byte_and_mtime_swap_is_detected(swap_workspace):
    before = swap_workspace.path.stat()
    forged = b"XriginalXartifactXbytes"
    assert len(forged) == before.st_size
    swap_workspace.path.write_bytes(forged)
    # Restore the original mtime: metadata forgery cannot hide the swap
    # because the recheck hashes the open handle, never trusts mtime.
    os.utime(swap_workspace.path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert _single_recheck(swap_workspace).status == "changed"


def test_artifact_locator_swap_and_missing_file_are_detected(swap_workspace):
    moved = swap_workspace.root / "elsewhere.txt"
    swap_workspace.path.rename(moved)
    assert _single_recheck(swap_workspace).status == "missing"
    # Swap a different file into the recorded locator path.
    swap_workspace.path.write_bytes(b"a different artifact entirely")
    assert _single_recheck(swap_workspace).status == "changed"


@requires_symlinks
def test_artifact_symlink_swap_is_detected_from_open_handle(swap_workspace, tmp_path):
    secret = tmp_path / "outside-root-secret.txt"
    secret.write_bytes(b"never read through a swapped link")
    swap_workspace.path.unlink()
    swap_workspace.path.symlink_to(secret)
    result = _single_recheck(swap_workspace)
    # A swapped symlink is never reported as an unchanged artifact and
    # the recheck never claims the outside bytes matched.
    assert result.status in ("changed", "missing", "inaccessible", "ambiguous")
    assert result.observed_sha256 != swap_workspace.digest.sha256 or (
        result.status != "unchanged"
    )


# =========================================================================
# Staleness: stale evidence cannot verify; a fresh recheck can
# =========================================================================


def test_stale_evidence_cannot_verify_and_fresh_recheck_appends_verified(
    db, tmp_path
):
    workspace = tmp_path / "code-workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    # Passed verification, then a later edit: evidence is stale.
    assert record_terminal_result(
        command="python -m pytest -q",
        cwd=workspace,
        session_id="s1",
        exit_code=0,
        output="all green",
    ) is not None
    assert mark_workspace_edited(
        session_id="s1", cwd=workspace, paths=[str(workspace / "calc.py")]
    ) is not None
    # The ledger label claims verified — an untrusted source claim.
    db.record_turn_outcome(_turn_record(outcome="verified"))

    issuer = build_receipt_issuer(db)
    original = issuer.issue(ReceiptSourceKey("turn", "s1:t1"))
    assert original.status == "completed_unverified"

    # A fresh passed verification after the last edit.
    assert record_terminal_result(
        command="python -m pytest -q",
        cwd=workspace,
        session_id="s1",
        exit_code=0,
        output="all green again",
    ) is not None
    observation = issuer.recheck(original.receipt_id)
    assert observation.status == "verified"
    assert observation.receipt_id == original.receipt_id
    # The original receipt and its terminal status never changed.
    stored = issuer.store.get(original.receipt_id)
    assert stored == original
    assert stored.status == "completed_unverified"


# =========================================================================
# Unknown effects dominate and are never blindly retried
# =========================================================================


def test_unknown_effect_dominates_failure_and_blocking_without_retry(db):
    db.record_turn_outcome(
        _turn_record(outcome="failed", outcome_reason="handler failed")
    )
    journal = OperationJournal(db)
    journal.create(
        operation_id="op-unknown",
        kind="message_send",
        session_id="s1",
        turn_id="t1",
        tool_call_id="call-send",
    )
    journal.transition(
        "op-unknown",
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )
    journal.transition(
        "op-unknown",
        from_states={"running"},
        to_state="unknown",
        effect_disposition="unknown",
    )
    issuer = build_receipt_issuer(db)
    receipt = issuer.issue(ReceiptSourceKey("turn", "s1:t1"))
    # Ambiguous landing dominates the known failure.
    assert receipt.status == "unknown_effect"

    # The receipt path never retried the operation: the journal row is
    # still in its unknown state after issue and recheck.
    issuer.recheck(receipt.receipt_id)
    op = journal.get("op-unknown")
    assert op.state == "unknown"
    assert op.effect_disposition == "unknown"

    # The real CLI service renders the no-retry warning.
    from hades_cli.receipts import run_argv

    shown = run_argv(["show", receipt.receipt_id])
    assert shown.exit_code == 0
    assert "unknown_effect" in shown.stdout
    assert "do not retry" in shown.stdout.lower()


# =========================================================================
# Forgery: artifacts, signatures, legacy imports, prose, self-scoring
# =========================================================================


def test_forged_artifact_and_model_done_text_never_verify(db, tmp_path):
    workspace = tmp_path / "forge-workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    target = workspace / "result.txt"
    target.write_text("claimed deliverable")
    assert record_terminal_result(
        command="python -m pytest -q",
        cwd=workspace,
        session_id="s1",
        exit_code=0,
        output="all green",
    ) is not None
    catalog = ArtifactCatalog(db)
    digest = catalog.register_path(
        target,
        source_kind="execute_code",
        source_ref="s1:t1:call-1",
        allowed_roots=(workspace,),
    )
    # Forge the artifact bytes after registration; the model says done.
    target.write_text("forged bytes that no longer match the digest")
    db.record_turn_outcome(
        _turn_record(outcome="verified", outcome_reason="model says done")
    )
    issuer = build_receipt_issuer(db, allowed_roots=(workspace,))
    receipt = issuer.issue(ReceiptSourceKey("turn", "s1:t1"))
    assert receipt.status == "failed"
    assert receipt.status != "verified"
    assert any(a.artifact_id == digest.artifact_id for a in receipt.artifacts)


def test_imported_legacy_verified_is_downgraded_until_recheck(tmp_path):
    legacy = tmp_path / "legacy-home"
    legacy.mkdir()
    db_path = legacy / "state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_V1_RECEIPTS_DDL)
        conn.execute(
            "INSERT INTO receipts (receipt_id, mission_id, status, objective, "
            "constraints_json, execution_ids_json, transaction_ids_json, "
            "before_after_json, claims_json, verifier_json, evidence_json, "
            "artifacts_json, uncertainty_json, freshness_json, content_hash, "
            "signature_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-e2e-1",
                "m1",
                "verified",
                "Deliver the weekly report",
                json.dumps(["no external send"]),
                json.dumps(["ex1"]),
                json.dumps(["tx1"]),
                json.dumps({"before": {}, "after": {}}),
                json.dumps(
                    [
                        {
                            "statement": "weekly report delivered",
                            "verdict": "satisfied",
                            "required": True,
                        }
                    ]
                ),
                json.dumps({"verifier_id": "workflow.end-state", "passed": True}),
                json.dumps([{"kind": "file", "path": "report.md"}]),
                json.dumps([]),
                json.dumps([]),
                json.dumps({"fresh_until": None}),
                "v1hash-e2e-1",
                json.dumps(
                    {
                        "provider": "legacy-signer",
                        "key_id": "k1",
                        "algorithm": "ed25519",
                        "signature": "c2lnbmF0dXJl",
                        "signed_at": 1721000000,
                    }
                ),
                1721000000,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    session_db = SessionDB(db_path=db_path)
    try:
        migrated = ReceiptStore(session_db).get("legacy-e2e-1")
        assert migrated is not None
        # A legacy verified row plus its legacy signature never re-enter
        # as verified: provenance is imported untrusted, truth is not.
        assert migrated.status == "completed_unverified"
        assert any("recheck" in item for item in migrated.uncertainty)
    finally:
        session_db.close()


def test_self_scoring_can_never_verify(db):
    db.record_turn_outcome(_turn_record())
    issuer = build_receipt_issuer(db)
    snapshot = issuer.sources.for_key(ReceiptSourceKey("turn", "s1:t1")).snapshot()

    class _SelfScorer:
        scorer_id = snapshot.producer_id  # scores its own output
        scorer_version = "1.0"
        supported_outcome_kinds = frozenset({snapshot.requested_outcome.outcome_kind})

        def evaluate(self, snap):
            return ScorerEvaluation(passed=True)

    service = ReceiptScoringService(ScorerRegistry())
    service.register(_SelfScorer())
    with pytest.raises(ScorerIndependenceError):
        service.decide(snapshot)


# =========================================================================
# Redacted public export with planted canaries
# =========================================================================


def test_public_export_has_no_canaries_and_every_hash_validates(db, tmp_path):
    home = Path(os.environ["HADES_HOME"])
    store = ReceiptStore(db)
    evidence = build_evidence_digest(
        evidence_kind="verification_check",
        source_ref="verification_evidence.db:check:e2e",
        producer_id="hades.verification",
        observed_at="2026-07-16T10:00:00Z",
        summary="delivery check passed",
        payload_hash=canonical_content_hash({"check": "delivery"}),
    )
    claim = build_claim(
        statement="the report artifact was produced",
        evidence_ids=(evidence.evidence_id,),
        verdict="satisfied",
    )
    receipt = store.insert(
        build_receipt(
            source=ReceiptSourceKey("turn", "s1:t9"),
            subject_kind="turn",
            subject_id="s1:t9",
            session_id="s1",
            turn_id="t9",
            requested_outcome=build_requested_outcome(
                outcome_kind="code_change",
                description="produce the report artifact",
                producer_id="hades.turn-ledger",
            ),
            status="completed_unverified",
            claims=(claim,),
            evidence=(evidence,),
            scorer_id="hades.receipts.default",
            scorer_version="1.0",
            decided_at="2026-07-16T10:00:00Z",
        )
    )
    # Plant canaries in the profile the exporter must never leak.
    (home / "sk-live-secret-canary.txt").write_text("sk-live-secret-canary")
    out = tmp_path / "export" / "receipt.json"
    out.parent.mkdir()
    exported = ReceiptExporter(store).export(receipt.receipt_id, out)
    text = Path(exported).read_text(encoding="utf-8")
    assert "sk-live-secret-canary" not in text
    assert home.as_posix() not in text
    assert str(home) not in text
    assert "artifact_locations" not in text
    # Every canonical content hash independently validates.
    assert verify_export_hashes(exported)


# =========================================================================
# Retention: holds refuse, tombstones append, expired locators degrade
# =========================================================================


def test_retention_refuses_holds_then_prunes_with_tombstone(db):
    store = ReceiptStore(db)
    evidence = build_evidence_digest(
        evidence_kind="turn_classification",
        source_ref="state.db:turn_outcomes:s1:t-old",
        producer_id="hades.turn-ledger",
        observed_at="2024-01-01T00:00:00Z",
        summary="old turn outcome",
        payload_hash=canonical_content_hash({"turn": "t-old"}),
    )
    claim = build_claim(
        statement="the old turn completed",
        evidence_ids=(evidence.evidence_id,),
        verdict="satisfied",
    )
    old = store.insert(
        build_receipt(
            source=ReceiptSourceKey("turn", "s1:t-old"),
            subject_kind="turn",
            subject_id="s1:t-old",
            session_id="s1",
            turn_id="t-old",
            requested_outcome=build_requested_outcome(
                outcome_kind="turn_outcome",
                description="an old turn",
                producer_id="hades.turn-ledger",
            ),
            status="completed_unverified",
            claims=(claim,),
            evidence=(evidence,),
            scorer_id="hades.receipts.default",
            scorer_version="1.0",
            decided_at="2024-01-01T00:00:00Z",
        )
    )
    holds: list[RetentionHold] = []
    service = ReceiptRetentionService(store, holds=lambda: list(holds))
    plan = service.plan()
    assert old.receipt_id in plan.receipt_ids
    # A hold added between plan and prune refuses the prune outright.
    holds.append(RetentionHold(old.receipt_id, "user", "operator audit hold"))
    with pytest.raises(RetentionHoldError):
        service.prune(plan.plan_id, plan.plan_hash)
    assert store.get(old.receipt_id) is not None
    # A fresh plan lists the blocker instead of the held receipt.
    blocked = service.plan()
    assert old.receipt_id not in blocked.receipt_ids
    assert any(b.receipt_id == old.receipt_id for b in blocked.blockers)

    holds.clear()
    plan = service.plan()
    assert old.receipt_id in plan.receipt_ids
    service.prune(plan.plan_id, plan.plan_hash)
    assert store.get(old.receipt_id) is None
    tombstones = store.list_tombstones()
    assert any(t.receipt_id == old.receipt_id for t in tombstones)
    assert any(t.receipt_content_hash == old.content_hash for t in tombstones)


def test_expired_locator_recheck_is_unverified_not_invented_failure(db, tmp_path):
    home = Path(os.environ["HADES_HOME"])
    workflows_db = home / "workflows.db"
    artifact_root = tmp_path / "retained-deliverables"
    artifact_root.mkdir()
    artifact_path = artifact_root / "deliverable.txt"
    artifact_path.write_text("published deliverable\n")
    catalog = ArtifactCatalog(db)
    digest = catalog.register_path(
        artifact_path,
        source_kind="mission",
        source_ref="m1:artifact",
        allowed_roots=(artifact_root,),
    )
    conn = sqlite3.connect(workflows_db)
    try:
        for statement in _MISSIONS_DDL:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO missions (mission_id, profile, objective, "
            "constraints_json, authority_json, evidence_json, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "m1",
                "default",
                "publish the deliverable",
                json.dumps([]),
                json.dumps({}),
                json.dumps({"artifact_ids": [digest.artifact_id]}),
                "completed",
                _T0,
                _T0 + 60,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    issuer = build_receipt_issuer(
        db,
        workflows_db_path=workflows_db,
        profile="default",
        allowed_roots=(artifact_root,),
    )
    original = issuer.issue(ReceiptSourceKey("mission", "m1"))

    # Locator retention pruned the raw location; the bytes may be gone.
    def _prune(conn):
        conn.execute(
            "DELETE FROM artifact_locations WHERE artifact_id = ?",
            (digest.artifact_id,),
        )

    db._execute_write(_prune)
    observation = issuer.recheck(original.receipt_id)
    assert observation.status == "completed_unverified"
    assert observation.status != "failed"
    assert any("no recheckable location" in u for u in observation.uncertainty)


# =========================================================================
# Profile isolation
# =========================================================================


def test_profile_b_gets_no_result_for_profile_a_ids(db, tmp_path, monkeypatch):
    db.record_turn_outcome(_turn_record())
    catalog_a = ArtifactCatalog(db)
    artifact = catalog_a.register_bytes(
        b"profile A artifact bytes",
        source_kind="turn",
        source_ref="s1:t1:artifact",
        display_name="a.txt",
    )
    issuer = build_receipt_issuer(db)
    receipt = issuer.issue(ReceiptSourceKey("turn", "s1:t1"))

    home_b = tmp_path / "profile-b-home"
    home_b.mkdir()
    db_b = SessionDB(db_path=home_b / "state.db")
    try:
        store_b = ReceiptStore(db_b)
        assert store_b.get(receipt.receipt_id) is None
        assert store_b.find_by_source(ReceiptSourceKey("turn", "s1:t1")) is None
        assert ArtifactCatalog(db_b).get(artifact.artifact_id) is None
    finally:
        db_b.close()

    # The real CLI under profile B finds nothing for A's receipt ID.
    from hades_cli.receipts import run_argv

    monkeypatch.setenv("HADES_HOME", str(home_b))
    result = run_argv(["show", receipt.receipt_id])
    assert result.exit_code == 2
    assert "Traceback" not in result.stdout
