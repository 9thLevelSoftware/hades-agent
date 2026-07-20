"""Tests for the mission-domain evidence manifest and end-state checks.

Ported from the provisional ``tests/agent/test_receipts.py`` when the
canonical receipt contract replaced the vertical-slice ``agent.receipts``
implementation. The provisional SessionDB persistence, hashing, and
issue/recheck tests were superseded by ``test_receipt_store.py``,
``test_receipt_models.py``, and ``test_receipt_ingest.py``; the
mission-domain behavior under test here moved unchanged to
``agent.mission_evidence``.
"""

from __future__ import annotations

import tempfile
from itertools import product
from pathlib import Path
from typing import Any

import pytest

from agent.mission_evidence import (
    EvidenceManifest,
    MissionEvidenceSnapshot,
    WorkflowEndStateScorer,
    collect_artifact_evidence,
    validate_evidence_manifest,
)


_ALL_CHECKS = (
    "workflow_succeeded",
    "all_effects_settled",
    "fresh_verification",
    "artifacts_exist",
    "outbox_confirmed",
)


def _manifest(**overrides: Any) -> EvidenceManifest:
    values: dict[str, Any] = {
        "checks": _ALL_CHECKS,
        "artifact_paths": ("build/output.txt",),
        "outbox_ids": ("outbox-1",),
    }
    values.update(overrides)
    return EvidenceManifest(**values)


def _snapshot(**overrides: Any) -> MissionEvidenceSnapshot:
    values: dict[str, Any] = {
        "mission_id": "mission-receipt-test",
        "objective": "Prove the requested change",
        "constraints": ("stay in scope",),
        "execution_ids": ("execution-1",),
        "transaction_ids": ("transaction-1",),
        "before_after": {"claimed": "after"},
        "claims": {"model": "done"},
        "manifest": _manifest(),
        "execution_statuses": ("succeeded",),
        "authority_blocked": False,
        "review_blocked": False,
        "operation_phases": ("committed",),
        "transaction_phases": ("committed",),
        "outbox_statuses": {"outbox-1": "confirmed"},
        "verification": {
            "status": "passed",
            "timestamp": "2026-07-16T12:00:00+00:00",
            "source": "verification_evidence",
        },
        "artifacts": ({
            "path": "/workspace/build/output.txt",
            "required_path": "build/output.txt",
            "exists": True,
            "within_allowed_root": True,
            "size": 12,
            "sha256": "a" * 64,
            "mtime": 1,
        },),
    }
    values.update(overrides)
    return MissionEvidenceSnapshot(**values)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"execution_statuses": ("failed",)}, "failed"),
        ({"authority_blocked": True}, "blocked"),
        ({"review_blocked": True}, "blocked"),
        ({"operation_phases": ("unknown_effect",)}, "unknown_effect"),
        ({"transaction_phases": ("unknown_effect",)}, "unknown_effect"),
        ({"outbox_statuses": {"outbox-1": "unknown"}}, "unknown_effect"),
        ({"verification": {"status": "unverified"}}, "completed_unverified"),
        ({"verification": {"status": "stale"}}, "completed_unverified"),
        ({"artifacts": ()}, "completed_unverified"),
        ({"outbox_statuses": {"outbox-1": "pending"}}, "completed_unverified"),
        (
            {
                "claims": {"model": "done"},
                "verification": {"status": "unverified"},
            },
            "completed_unverified",
        ),
        ({}, "verified"),
    ],
)
def test_end_state_scorer_truth_table(overrides: dict[str, Any], expected: str) -> None:
    assert WorkflowEndStateScorer().score(_snapshot(**overrides)).status == expected


_FALSE_SUCCESS_CASES: list[dict[str, Any]] = []
for verification_status, artifact_mode, outbox_status, effect_phase in product(
    ("unverified", "stale", "failed", "not_applicable", "missing"),
    ("missing", "outside", "zero_size"),
    ("pending", "failed", "cancelled", "missing"),
    ("pending", "failed", "cancelled"),
):
    if artifact_mode == "missing":
        artifact: tuple[dict[str, Any], ...] = ()
    elif artifact_mode == "outside":
        artifact = ({
            "path": "/outside/output.txt", "required_path": "build/output.txt",
            "exists": True, "within_allowed_root": False, "size": 12,
            "sha256": "b" * 64, "mtime": 1,
        },)
    else:
        artifact = ({
            "path": "/workspace/build/output.txt", "required_path": "build/output.txt",
            "exists": True, "within_allowed_root": True, "size": 0,
            "sha256": "c" * 64, "mtime": 1,
        },)
    _FALSE_SUCCESS_CASES.append({
        "verification": {"status": verification_status},
        "artifacts": artifact,
        "outbox_statuses": {"outbox-1": outbox_status},
        "transaction_phases": (effect_phase,),
    })


@pytest.mark.parametrize("overrides", _FALSE_SUCCESS_CASES[:50])
def test_false_success_corpus_never_returns_verified(overrides: dict[str, Any]) -> None:
    assert WorkflowEndStateScorer().score(_snapshot(**overrides)).status != "verified"


def test_unknown_manifest_check_blocks_mission_start() -> None:
    with pytest.raises(ValueError, match="unsupported evidence check"):
        validate_evidence_manifest({"checks": ["workflow_succeeded", "vibes"]})


def _symlinks_supported() -> bool:
    with tempfile.TemporaryDirectory() as probe:
        target = Path(probe) / "target.txt"
        target.write_bytes(b"t")
        try:
            (Path(probe) / "link.txt").symlink_to(target)
        except OSError:
            return False
    return True


requires_symlinks = pytest.mark.skipif(
    not _symlinks_supported(),
    reason="platform cannot create symlinks without extra privilege",
)


def test_artifact_observation_hashes_only_allowed_regular_files(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    artifact = root / "build" / "output.txt"
    artifact.parent.mkdir()
    # write_bytes: the frozen sha256 below is over exactly b"proof\n" —
    # write_text would produce b"proof\r\n" under Windows newline translation.
    artifact.write_bytes(b"proof\n")
    evidence = collect_artifact_evidence(_manifest(), allowed_roots=(root,))

    assert evidence == ({
        "required_path": "build/output.txt", "path": str(artifact.resolve()),
        "exists": True, "within_allowed_root": True, "size": len("proof\n"),
        "sha256": "f6ed42a9d765eeb230a069bbc3d5dc346b2669594bb0b83cc6d14d5d967b8961",
        "mtime": artifact.stat().st_mtime_ns,
    },)


@requires_symlinks
def test_artifact_observation_rejects_symlink_escape(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (root / "build").mkdir()
    (root / "build" / "output.txt").symlink_to(outside)

    with pytest.raises(ValueError, match="allowed roots"):
        collect_artifact_evidence(_manifest(), allowed_roots=(root,))
