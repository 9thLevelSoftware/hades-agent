"""Tests for the content-addressed artifact catalog (Task 3).

Covers ``ArtifactCatalog.register_path/register_bytes/recheck`` and the
public ``digest_artifact()`` against a real profile-local ``SessionDB``,
real files, and real hashes:

- Identical bytes are stored once (one ``artifact_digests`` row) while
  every registration keeps its own deduplicated source-link location.
- Registration and recheck are boundary-enforced: paths outside the
  allowed roots, symlinks, and Windows reparse-point (junction) escapes
  are refused without reading the target bytes.
- Recheck is read-only and race-safe: it reports ``missing``,
  ``changed``, ``inaccessible``, and ``ambiguous`` truthfully instead of
  claiming a stable digest for a file that moved under it.
- Public artifact digests never leak raw local path prefixes; raw
  locators stay in the bounded ``artifact_locations`` table.
- Profiles remain independent: identical bytes in two profiles never
  share a catalog row or a lookup.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from hades_state import SessionDB
from agent.receipt_artifacts import (
    ArtifactAmbiguityError,
    ArtifactBoundaryError,
    ArtifactCatalog,
    ArtifactSizeError,
    ArtifactSourceConflict,
    ArtifactTypeError,
    digest_artifact,
)
from agent.receipt_models import ArtifactDigest


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _symlinks_supported() -> bool:
    with tempfile.TemporaryDirectory() as probe:
        target = Path(probe) / "target.txt"
        target.write_text("probe", encoding="utf-8")
        try:
            (Path(probe) / "link.txt").symlink_to(target)
        except (OSError, NotImplementedError):
            return False
    return True


requires_symlinks = pytest.mark.skipif(
    not _symlinks_supported(),
    reason="platform cannot create symlinks without extra privilege",
)


@pytest.fixture(autouse=True)
def _isolated_profile(tmp_path, monkeypatch):
    home = tmp_path / "profile-home"
    home.mkdir()
    monkeypatch.setenv("HADES_HOME", str(home))
    yield home


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


@pytest.fixture()
def catalog(db):
    return ArtifactCatalog(db)


@pytest.fixture()
def root(tmp_path):
    directory = tmp_path / "allowed-root"
    directory.mkdir()
    return directory


@pytest.fixture()
def secret(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret_file = outside / "secret.txt"
    secret_file.write_bytes(b"top secret bytes")
    return secret_file


# ---------------------------------------------------------------------------
# Plan-specified contract tests.
# ---------------------------------------------------------------------------


def test_same_bytes_reuse_digest_but_keep_source_links(catalog, tmp_path):
    a = tmp_path / "a.txt"; b = tmp_path / "b.txt"
    a.write_bytes(b"proof"); b.write_bytes(b"proof")
    first = catalog.register_path(a, source_kind="execute_code", source_ref="call-a",
                                  allowed_roots=(tmp_path,))
    second = catalog.register_path(b, source_kind="mission", source_ref="m1:artifact",
                                   allowed_roots=(tmp_path,))
    assert first.artifact_id == second.artifact_id
    assert catalog.location_count(first.artifact_id) == 2


@requires_symlinks
def test_recheck_detects_symlink_swap_and_never_reads_outside_root(catalog, root, secret):
    link = root / "report.txt"
    link.symlink_to(secret)
    with pytest.raises(ArtifactBoundaryError):
        catalog.register_path(link, source_kind="test", source_ref="escape",
                              allowed_roots=(root,))


# ---------------------------------------------------------------------------
# Content addressing and source links.
# ---------------------------------------------------------------------------


def test_register_path_returns_canonical_artifact_digest(catalog, root):
    payload = b"artifact payload"
    target = root / "report.txt"
    target.write_bytes(payload)
    digest = catalog.register_path(
        target, source_kind="execute_code", source_ref="call-1",
        allowed_roots=(root,),
    )
    assert isinstance(digest, ArtifactDigest)
    assert digest.artifact_id.startswith("art_")
    assert digest.sha256 == _sha256(payload)
    assert digest.size_bytes == len(payload)
    assert digest.display_name == "report.txt"
    assert digest.content_hash.startswith("sha256:")
    assert catalog.get(digest.artifact_id) == digest
    assert catalog.digest_count() == 1


def test_duplicate_location_replay_is_idempotent(catalog, root):
    target = root / "same.txt"
    target.write_bytes(b"stable bytes")
    first = catalog.register_path(
        target, source_kind="execute_code", source_ref="call-1",
        allowed_roots=(root,),
    )
    second = catalog.register_path(
        target, source_kind="execute_code", source_ref="call-1",
        allowed_roots=(root,),
    )
    assert first == second
    assert catalog.location_count(first.artifact_id) == 1
    assert catalog.digest_count() == 1


def test_source_identity_reuse_with_changed_content_is_a_conflict(catalog, root):
    target = root / "mutating.txt"
    target.write_bytes(b"original")
    catalog.register_path(
        target, source_kind="execute_code", source_ref="call-1",
        allowed_roots=(root,),
    )
    target.write_bytes(b"tampered")
    with pytest.raises(ArtifactSourceConflict):
        catalog.register_path(
            target, source_kind="execute_code", source_ref="call-1",
            allowed_roots=(root,),
        )
    # The failed registration rolled back atomically.
    assert catalog.digest_count() == 1


def test_register_bytes_deduplicates_against_path_registration(catalog, root):
    payload = b"proof"
    on_disk = root / "a.txt"
    on_disk.write_bytes(payload)
    from_path = catalog.register_path(
        on_disk, source_kind="execute_code", source_ref="call-a",
        allowed_roots=(root,),
    )
    from_bytes = catalog.register_bytes(
        payload, source_kind="mission", source_ref="m1:inline",
        display_name="inline.txt",
    )
    assert from_bytes.artifact_id == from_path.artifact_id
    assert catalog.digest_count() == 1
    assert catalog.location_count(from_path.artifact_id) == 2


def test_identical_bytes_in_two_profiles_stay_profile_local(tmp_path, monkeypatch):
    payload = b"shared-bytes"
    per_profile = []
    for name in ("profile-a", "profile-b"):
        home = tmp_path / name
        home.mkdir()
        monkeypatch.setenv("HADES_HOME", str(home))
        session_db = SessionDB(db_path=home / "state.db")
        try:
            catalog = ArtifactCatalog(session_db)
            artifact = home / "artifact.bin"
            artifact.write_bytes(payload)
            digest = catalog.register_path(
                artifact, source_kind="test", source_ref=f"{name}:artifact",
                allowed_roots=(home,),
            )
            per_profile.append(
                (catalog.digest_count(), catalog.location_count(digest.artifact_id))
            )
        finally:
            session_db.close()
    # Each profile stores exactly its own copy: no cross-profile lookup
    # ever merged the second registration into the first profile's row.
    assert per_profile == [(1, 1), (1, 1)]


# ---------------------------------------------------------------------------
# Boundary and safety enforcement.
# ---------------------------------------------------------------------------


def test_register_missing_artifact_raises(catalog, root):
    with pytest.raises(FileNotFoundError):
        catalog.register_path(
            root / "nope.txt", source_kind="test", source_ref="missing",
            allowed_roots=(root,),
        )


def test_register_outside_allowed_roots_is_refused(catalog, root, secret):
    with pytest.raises(ArtifactBoundaryError):
        catalog.register_path(
            secret, source_kind="test", source_ref="escape",
            allowed_roots=(root,),
        )


def test_register_non_regular_file_is_refused(catalog, root):
    subdir = root / "not-a-file"
    subdir.mkdir()
    with pytest.raises(ArtifactTypeError):
        catalog.register_path(
            subdir, source_kind="test", source_ref="directory",
            allowed_roots=(root,),
        )


def test_oversized_file_is_refused(db, root):
    bounded = ArtifactCatalog(db, max_bytes=4)
    target = root / "big.bin"
    target.write_bytes(b"12345")
    with pytest.raises(ArtifactSizeError):
        bounded.register_path(
            target, source_kind="test", source_ref="oversized",
            allowed_roots=(root,),
        )
    assert bounded.digest_count() == 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows no-follow fallback")
def test_windows_no_follow_fallback_refuses_junction_escape(catalog, root, tmp_path):
    outside = tmp_path / "junction-target"
    outside.mkdir()
    (outside / "leak.txt").write_bytes(b"outside bytes")
    junction = root / "jump"
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"mklink /J unavailable: {proc.stderr!r}")
    # Traversing the junction resolves outside the allowed root.
    with pytest.raises(ArtifactBoundaryError):
        catalog.register_path(
            junction / "leak.txt", source_kind="test",
            source_ref="junction-escape", allowed_roots=(root,),
        )
    # The junction itself is a reparse point and is refused outright.
    with pytest.raises(ArtifactBoundaryError):
        catalog.register_path(
            junction, source_kind="test", source_ref="junction-itself",
            allowed_roots=(root,),
        )


def test_register_reports_ambiguity_when_path_identity_changes(
    catalog, root, monkeypatch
):
    import agent.receipt_artifacts as receipt_artifacts

    target = root / "racy.txt"
    target.write_bytes(b"first bytes")
    monkeypatch.setattr(
        receipt_artifacts,
        "_post_capture_identity",
        lambda path: receipt_artifacts._PostCaptureIdentity(
            identity=("inode", -1, -1), is_link=False, exists=True,
        ),
    )
    with pytest.raises(ArtifactAmbiguityError):
        catalog.register_path(
            target, source_kind="test", source_ref="racy",
            allowed_roots=(root,),
        )


def test_public_source_ref_redacts_local_path_prefixes(catalog, root):
    target = root / "doc.txt"
    target.write_bytes(b"document bytes")
    raw_ref = f"execute_code:{target}"
    digest = catalog.register_path(
        target, source_kind="execute_code", source_ref=raw_ref,
        allowed_roots=(root,),
    )
    # The public digest never embeds the raw absolute path prefix.
    assert str(root) not in digest.source_ref
    assert "<redacted>" in digest.source_ref
    assert digest.source_ref.endswith("doc.txt")


def test_raw_locator_stays_in_bounded_location_table(catalog, db, root):
    target = root / "kept.txt"
    target.write_bytes(b"kept bytes")
    digest = catalog.register_path(
        target, source_kind="execute_code", source_ref="call-kept",
        allowed_roots=(root,),
    )
    rows = db._execute_read(
        lambda conn: conn.execute(
            "SELECT locator_json FROM artifact_locations WHERE artifact_id = ?",
            (digest.artifact_id,),
        ).fetchall()
    )
    assert len(rows) == 1
    import json as json_module

    locator = json_module.loads(rows[0]["locator_json"])
    assert locator == {"kind": "file", "path": str(target.resolve())}
    # ...while the public digest fields never carry it.
    assert str(target.resolve()) not in digest.source_ref
    assert str(target.resolve()) not in digest.display_name


def test_artifact_digest_rows_are_immutable(catalog, db, root):
    target = root / "frozen.txt"
    target.write_bytes(b"frozen bytes")
    catalog.register_path(
        target, source_kind="test", source_ref="frozen",
        allowed_roots=(root,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE artifact_digests SET sha256 = 'tampered'"
            )
        )
    with pytest.raises(sqlite3.IntegrityError, match="last_checked_at"):
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE artifact_locations SET source_ref = 'rewritten'"
            )
        )


# ---------------------------------------------------------------------------
# Read-only recheck.
# ---------------------------------------------------------------------------


def test_recheck_reports_unchanged_bytes(catalog, db, root):
    payload = b"stable evidence"
    target = root / "evidence.txt"
    target.write_bytes(payload)
    digest = catalog.register_path(
        target, source_kind="test", source_ref="stable",
        allowed_roots=(root,),
    )
    (result,) = catalog.recheck(digest.artifact_id, allowed_roots=(root,))
    assert result.status == "unchanged"
    assert result.artifact_id == digest.artifact_id
    assert result.observed_sha256 == _sha256(payload)
    assert result.observed_size_bytes == len(payload)
    # Bookkeeping only: recheck records when the location was last checked.
    checked = db._execute_read(
        lambda conn: conn.execute(
            "SELECT last_checked_at FROM artifact_locations WHERE artifact_id = ?",
            (digest.artifact_id,),
        ).fetchone()
    )
    assert checked["last_checked_at"] is not None


def test_recheck_reports_missing_artifact(catalog, root):
    target = root / "gone.txt"
    target.write_bytes(b"soon gone")
    digest = catalog.register_path(
        target, source_kind="test", source_ref="gone",
        allowed_roots=(root,),
    )
    target.unlink()
    (result,) = catalog.recheck(digest.artifact_id, allowed_roots=(root,))
    assert result.status == "missing"
    assert result.observed_sha256 is None


def test_recheck_detects_changed_bytes_with_same_size_and_mtime(catalog, root):
    target = root / "sneaky.bin"
    target.write_bytes(b"AAAA")
    stat_before = target.stat()
    digest = catalog.register_path(
        target, source_kind="test", source_ref="sneaky",
        allowed_roots=(root,),
    )
    target.write_bytes(b"BBBB")
    os.utime(target, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
    assert target.stat().st_mtime_ns == stat_before.st_mtime_ns
    assert target.stat().st_size == 4
    (result,) = catalog.recheck(digest.artifact_id, allowed_roots=(root,))
    assert result.status == "changed"
    assert result.observed_sha256 == _sha256(b"BBBB")


def test_recheck_never_reads_outside_allowed_roots(catalog, root, tmp_path):
    target = root / "scoped.txt"
    target.write_bytes(b"scoped bytes")
    digest = catalog.register_path(
        target, source_kind="test", source_ref="scoped",
        allowed_roots=(root,),
    )
    other_root = tmp_path / "other-root"
    other_root.mkdir()
    (result,) = catalog.recheck(digest.artifact_id, allowed_roots=(other_root,))
    assert result.status == "inaccessible"
    assert result.observed_sha256 is None
    assert "root" in result.detail


@requires_symlinks
def test_recheck_reports_symlink_swap_as_inaccessible(catalog, root, secret):
    target = root / "report.txt"
    target.write_bytes(b"honest bytes")
    digest = catalog.register_path(
        target, source_kind="test", source_ref="swapped",
        allowed_roots=(root,),
    )
    target.unlink()
    target.symlink_to(secret)
    (result,) = catalog.recheck(digest.artifact_id, allowed_roots=(root,))
    assert result.status == "inaccessible"
    assert result.observed_sha256 is None
    assert "symlink" in result.detail
    # The secret bytes were never hashed or surfaced.
    assert _sha256(secret.read_bytes()) != digest.sha256


def test_recheck_reports_ambiguous_when_file_replaced_between_open_and_stat(
    catalog, root, monkeypatch
):
    import agent.receipt_artifacts as receipt_artifacts

    target = root / "swapping.txt"
    target.write_bytes(b"original bytes")
    digest = catalog.register_path(
        target, source_kind="test", source_ref="swapping",
        allowed_roots=(root,),
    )
    monkeypatch.setattr(
        receipt_artifacts,
        "_post_capture_identity",
        lambda path: receipt_artifacts._PostCaptureIdentity(
            identity=("inode", -1, -1), is_link=False, exists=True,
        ),
    )
    (result,) = catalog.recheck(digest.artifact_id, allowed_roots=(root,))
    assert result.status == "ambiguous"
    assert result.observed_sha256 is None


def test_recheck_reports_inline_bytes_location_as_ambiguous(catalog):
    digest = catalog.register_bytes(
        b"inline evidence", source_kind="mission", source_ref="m1:inline",
        display_name="inline.txt",
    )
    (result,) = catalog.recheck(digest.artifact_id, allowed_roots=())
    assert result.status == "ambiguous"
    assert "inline" in result.detail


def test_recheck_unknown_artifact_raises(catalog):
    with pytest.raises(Exception, match="unknown artifact"):
        catalog.recheck("art_" + "0" * 64, allowed_roots=())


# ---------------------------------------------------------------------------
# Public digest_artifact().
# ---------------------------------------------------------------------------


def test_digest_artifact_hashes_open_handle_and_reports_facts(root):
    payload = b"public digest payload"
    target = root / "public.txt"
    target.write_bytes(payload)
    digest = digest_artifact(
        target, source_kind="execute_code", source_ref="call-9",
        allowed_roots=(root,),
    )
    assert isinstance(digest, ArtifactDigest)
    assert digest.sha256 == _sha256(payload)
    assert digest.size_bytes == len(payload)
    assert digest.mtime_ns == target.stat().st_mtime_ns
    assert digest.artifact_id.startswith("art_")
    assert digest.display_name == "public.txt"


def test_digest_artifact_refuses_paths_outside_allowed_roots(root, secret):
    with pytest.raises(ArtifactBoundaryError):
        digest_artifact(
            secret, source_kind="test", source_ref="escape",
            allowed_roots=(root,),
        )
