"""Content-addressed artifact catalog and read-only recheck for receipts.

Owns bounded artifact registration, locator isolation, open-handle
hashing, and race-safe recheck for the Verified Outcome & Artifact
Receipts plan (Task 3):

- Identical bytes are stored once in ``artifact_digests``; every
  registration keeps its own deduplicated source link in the bounded
  ``artifact_locations`` table. Bytes are never copied into the catalog.
- Raw local locators (absolute paths) live ONLY in ``locator_json`` and
  are excluded from public export; the public :class:`ArtifactDigest`
  carries a redacted source reference and a basename display name.
- Capture is boundary-enforced and race-safe: parent directories are
  resolved and checked against the allowed roots, symlinks and Windows
  reparse points are refused without following them, ``O_NOFOLLOW`` is
  applied where available, SHA-256 streams from the same open handle
  that is statted, and inode/file-index plus size are re-checked after
  hashing. When the platform cannot provide a stable identity for a
  path that changed during capture, the result is ambiguity — never a
  claimed stable digest.
- Recheck is read-only: it reports ``unchanged``, ``missing``,
  ``changed``, ``inaccessible``, or ``ambiguous`` truthfully and writes
  only the ``last_checked_at`` bookkeeping column.

Consumes Task 1 models/hashing and Task 2 low-level ``SessionDB``
storage. Profiles remain independent: a catalog is bound to one
profile-local ``state.db`` and never looks up digests across
``HADES_HOME`` boundaries.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat as stat_module
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from agent.receipt_hashing import (
    canonical_content_hash,
    hash_hex,
    normalize_utc_timestamp,
)
from agent.receipt_models import ArtifactDigest, build_artifact_digest

if TYPE_CHECKING:  # pragma: no cover - typing only
    import sqlite3

    from hades_state import SessionDB

__all__ = [
    "ArtifactAmbiguityError",
    "ArtifactBoundaryError",
    "ArtifactCatalog",
    "ArtifactCatalogError",
    "ArtifactIntegrityError",
    "ArtifactRecheckResult",
    "ArtifactSizeError",
    "ArtifactSourceConflict",
    "ArtifactTypeError",
    "DEFAULT_MAX_ARTIFACT_BYTES",
    "digest_artifact",
]

# Registration streams bytes for hashing only (never copies them), but an
# unbounded file would still turn recheck into an unbounded read primitive.
DEFAULT_MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024

RecheckStatus = Literal[
    "unchanged", "missing", "changed", "inaccessible", "ambiguous"
]


class ArtifactCatalogError(RuntimeError):
    """Base error for artifact catalog failures."""


class ArtifactBoundaryError(ArtifactCatalogError):
    """A path escapes the allowed roots or is a symlink/reparse point."""


class ArtifactTypeError(ArtifactCatalogError):
    """The path does not name a regular file."""


class ArtifactSizeError(ArtifactCatalogError):
    """The artifact exceeds the catalog byte bound."""


class ArtifactAmbiguityError(ArtifactCatalogError):
    """The file changed during capture; no stable digest can be claimed."""


class ArtifactSourceConflict(ArtifactCatalogError):
    """A source identity was reused with different artifact content."""


class ArtifactIntegrityError(ArtifactCatalogError):
    """A persisted catalog row fails canonical hash recomputation."""


@dataclass(frozen=True)
class ArtifactRecheckResult:
    """Truthful outcome of one read-only location recheck."""

    artifact_id: str
    location_id: str
    source_kind: str
    source_ref: str
    status: RecheckStatus
    detail: str
    observed_sha256: str | None
    observed_size_bytes: int | None
    checked_at: str


# ---------------------------------------------------------------------------
# Redaction: public digests never leak raw local path prefixes.
# ---------------------------------------------------------------------------


def _redaction_prefixes() -> tuple[str, ...]:
    prefixes: set[str] = set()
    try:
        from hades_constants import get_hades_home

        prefixes.add(str(Path(get_hades_home()).expanduser().resolve()))
    except Exception:
        pass
    try:
        prefixes.add(str(Path.home().resolve()))
    except Exception:
        pass
    try:
        prefixes.add(str(Path(tempfile.gettempdir()).resolve()))
    except Exception:
        pass
    variants: set[str] = set()
    for prefix in prefixes:
        if not prefix or prefix == os.sep:
            continue
        variants.add(prefix)
        variants.add(prefix.replace("\\", "/"))
        variants.add(prefix.replace("/", "\\"))
    # Longest first so a home prefix nested under another prefix cannot
    # leave a partial absolute path behind.
    return tuple(sorted(variants, key=len, reverse=True))


def _redact_source_ref(source_ref: str) -> str:
    """Replace sensitive absolute path prefixes in a public source ref."""
    text = str(source_ref)
    for prefix in _redaction_prefixes():
        if prefix and prefix in text:
            text = text.replace(prefix, "<redacted>")
    return text


def _safe_display_name(name: object, fallback: str = "artifact") -> str:
    return os.path.basename(str(name or fallback)).strip() or fallback


def _guess_media_type(display_name: str) -> str | None:
    import mimetypes

    guessed, _ = mimetypes.guess_type(display_name)
    return guessed


def _now() -> str:
    return normalize_utc_timestamp(datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Safe open-handle capture.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PostCaptureIdentity:
    """Identity of the given path re-observed after hashing."""

    identity: tuple
    is_link: bool
    exists: bool


@dataclass(frozen=True)
class _Capture:
    sha256: str
    size_bytes: int
    mtime_ns: int
    real_path: Path


def _is_reparse_or_symlink(st: os.stat_result) -> bool:
    if stat_module.S_ISLNK(st.st_mode):
        return True
    attributes = getattr(st, "st_file_attributes", 0)
    reparse_flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & reparse_flag)


def _stat_identity(st: os.stat_result) -> tuple:
    """Best stable identity the platform offers for a stat result."""
    if st.st_ino and st.st_dev:
        return ("inode", st.st_dev, st.st_ino)
    # No inode/file-index primitive: fall back to observable facts. A
    # mismatch here reports ambiguity rather than claiming stability.
    return ("stat", st.st_size, st.st_mtime_ns)


def _post_capture_identity(path: str) -> _PostCaptureIdentity:
    """Re-observe the given path after hashing (module-level test seam)."""
    try:
        st = os.lstat(path)
    except OSError:
        return _PostCaptureIdentity(identity=(), is_link=False, exists=False)
    return _PostCaptureIdentity(
        identity=_stat_identity(st),
        is_link=_is_reparse_or_symlink(st),
        exists=True,
    )


def _hash_open_handle(fd: int) -> tuple[str, int]:
    """Stream SHA-256 from an already-open handle; never reopen the path."""
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = os.read(fd, _HASH_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


def _normalized(path: object) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _is_under(child: Path, roots: tuple[Path, ...]) -> bool:
    child_cmp = os.path.normcase(str(child))
    for root in roots:
        root_cmp = os.path.normcase(str(root))
        try:
            if os.path.commonpath((child_cmp, root_cmp)) == root_cmp:
                return True
        except ValueError:
            continue
    return False


def _resolve_roots(allowed_roots: tuple) -> tuple[Path, ...]:
    return tuple(
        Path(os.path.realpath(str(_normalized(root)))) for root in allowed_roots
    )


def _safe_capture(
    path: object, allowed_roots: tuple, *, max_bytes: int
) -> _Capture:
    """Hash one bounded regular file without following links or races.

    Order matters: refuse a symlink/reparse final component before any
    open, resolve parent directories and boundary-check against the
    allowed roots, open with ``O_NOFOLLOW`` where available, confirm
    regular-file identity and size with ``fstat`` on the open handle,
    stream SHA-256 from that same handle, then re-check the handle size
    and the path identity. Any instability is reported as ambiguity.
    """
    if not allowed_roots:
        raise ArtifactBoundaryError(
            "artifact capture requires at least one allowed root"
        )
    given = _normalized(path)
    st_link = os.lstat(given)  # FileNotFoundError propagates when absent
    if _is_reparse_or_symlink(st_link):
        raise ArtifactBoundaryError(
            f"refusing symlink/reparse point: {given.name!r}"
        )
    if not stat_module.S_ISREG(st_link.st_mode):
        raise ArtifactTypeError(f"not a regular file: {given.name!r}")
    real = Path(os.path.realpath(str(given)))
    roots = _resolve_roots(tuple(allowed_roots))
    if not _is_under(real, roots):
        raise ArtifactBoundaryError(
            "artifact path resolves outside the allowed roots"
        )
    flags = os.O_RDONLY
    for flag_name in ("O_BINARY", "O_NOFOLLOW", "O_CLOEXEC", "O_NOINHERIT"):
        flags |= getattr(os, flag_name, 0)
    try:
        fd = os.open(str(real), flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", errno.ELOOP)):
            raise ArtifactBoundaryError(
                f"refusing symlink swapped in during capture: {real.name!r}"
            ) from exc
        raise
    try:
        st_handle = os.fstat(fd)
        if not stat_module.S_ISREG(st_handle.st_mode):
            raise ArtifactTypeError(f"not a regular file: {real.name!r}")
        if st_handle.st_size > max_bytes:
            raise ArtifactSizeError(
                f"artifact is {st_handle.st_size} bytes; the catalog bound "
                f"is {max_bytes}"
            )
        sha256, hashed_bytes = _hash_open_handle(fd)
        st_after = os.fstat(fd)
        if (
            hashed_bytes != st_handle.st_size
            or st_after.st_size != st_handle.st_size
        ):
            raise ArtifactAmbiguityError(
                "file size changed while hashing; the digest is not stable"
            )
        post = _post_capture_identity(str(given))
        if not post.exists:
            raise ArtifactAmbiguityError(
                "file disappeared during capture; the path no longer names "
                "the hashed bytes"
            )
        if post.is_link:
            raise ArtifactBoundaryError(
                "path was swapped to a symlink during capture"
            )
        if post.identity != _stat_identity(st_handle):
            raise ArtifactAmbiguityError(
                "file was replaced between open and stat; the digest does "
                "not describe the current path"
            )
    finally:
        os.close(fd)
    return _Capture(
        sha256=sha256,
        size_bytes=st_handle.st_size,
        mtime_ns=st_handle.st_mtime_ns,
        real_path=real,
    )


# ---------------------------------------------------------------------------
# Public storeless digest (frozen canonical interface).
# ---------------------------------------------------------------------------


def digest_artifact(
    path: Path,
    *,
    source_kind: str,
    source_ref: str,
    allowed_roots: tuple[Path, ...],
) -> ArtifactDigest:
    """Safely hash *path* into a canonical :class:`ArtifactDigest`."""
    capture = _safe_capture(
        path, tuple(allowed_roots), max_bytes=DEFAULT_MAX_ARTIFACT_BYTES
    )
    display_name = _safe_display_name(capture.real_path.name)
    return build_artifact_digest(
        source_kind=source_kind,
        source_ref=_redact_source_ref(source_ref),
        display_name=display_name,
        media_type=_guess_media_type(display_name),
        size_bytes=capture.size_bytes,
        sha256=capture.sha256,
        mtime_ns=capture.mtime_ns,
        captured_at=_now(),
    )


# ---------------------------------------------------------------------------
# The profile-local catalog.
# ---------------------------------------------------------------------------

_INSERT_DIGEST_SQL = (
    "INSERT INTO artifact_digests (artifact_id, sha256, size_bytes, "
    "media_type, display_name, captured_at, content_hash) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)

_INSERT_LOCATION_SQL = (
    "INSERT INTO artifact_locations (location_id, artifact_id, source_kind, "
    "source_ref, locator_json, locator_hash, created_at, last_checked_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)"
)


class ArtifactCatalog:
    """Content-addressed artifact digests over a profile-local ``SessionDB``."""

    def __init__(
        self,
        db: "SessionDB",
        *,
        max_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    ) -> None:
        self._db = db
        self._max_bytes = int(max_bytes)
        self._owns_db = False

    @classmethod
    def for_profile(cls) -> "ArtifactCatalog":
        """Fresh catalog bound to the active profile's ``state.db``.

        The returned catalog owns its database handle; call
        :meth:`close` when finished. Resolution goes through
        ``get_hades_home()`` and never crosses profile boundaries.
        """
        from hades_constants import get_hades_home
        from hades_state import SessionDB

        db_path = Path(get_hades_home()) / "state.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        catalog = cls(SessionDB(db_path=db_path))
        catalog._owns_db = True
        return catalog

    def close(self) -> None:
        """Close the underlying handle when this catalog owns it."""
        if self._owns_db and self._db.is_open:
            self._db.close()

    def __enter__(self) -> "ArtifactCatalog":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ── Registration ──

    def register_path(
        self,
        path: Path,
        *,
        source_kind: str,
        source_ref: str,
        allowed_roots: tuple[Path, ...],
        display_name: str | None = None,
        media_type: str | None = None,
    ) -> ArtifactDigest:
        """Register one bounded file without duplicating its bytes."""
        capture = _safe_capture(
            path, tuple(allowed_roots), max_bytes=self._max_bytes
        )
        name = _safe_display_name(display_name or capture.real_path.name)
        # The raw absolute path is a private locator, never a public field.
        locator = {"kind": "file", "path": str(capture.real_path)}
        return self._register(
            sha256=capture.sha256,
            size_bytes=capture.size_bytes,
            source_kind=source_kind,
            source_ref=source_ref,
            display_name=name,
            media_type=media_type or _guess_media_type(name),
            locator=locator,
        )

    def register_bytes(
        self,
        data: bytes,
        *,
        source_kind: str,
        source_ref: str,
        display_name: str,
        media_type: str | None = None,
    ) -> ArtifactDigest:
        """Register already-in-memory bytes (no durable re-checkable path)."""
        payload = bytes(data)
        if len(payload) > self._max_bytes:
            raise ArtifactSizeError(
                f"artifact is {len(payload)} bytes; the catalog bound is "
                f"{self._max_bytes}"
            )
        name = _safe_display_name(display_name)
        return self._register(
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            source_kind=source_kind,
            source_ref=source_ref,
            display_name=name,
            media_type=media_type or _guess_media_type(name),
            locator={"kind": "bytes"},
        )

    def _register(
        self,
        *,
        sha256: str,
        size_bytes: int,
        source_kind: str,
        source_ref: str,
        display_name: str,
        media_type: str | None,
        locator: dict,
    ) -> ArtifactDigest:
        public_ref = _redact_source_ref(source_ref)
        captured_at = _now()
        locator_hash = canonical_content_hash(locator)
        locator_json = json.dumps(
            locator, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

        def _do(conn: "sqlite3.Connection") -> ArtifactDigest:
            row = conn.execute(
                "SELECT * FROM artifact_digests "
                "WHERE sha256 = ? AND size_bytes = ?",
                (sha256, size_bytes),
            ).fetchone()
            if row is None:
                digest = build_artifact_digest(
                    source_kind=source_kind,
                    source_ref=public_ref,
                    display_name=display_name,
                    media_type=media_type,
                    size_bytes=size_bytes,
                    sha256=sha256,
                    # mtime is a volatile per-location fact; the canonical
                    # deduplicated digest never depends on it.
                    mtime_ns=None,
                    captured_at=captured_at,
                )
                conn.execute(
                    _INSERT_DIGEST_SQL,
                    (
                        digest.artifact_id,
                        digest.sha256,
                        digest.size_bytes,
                        digest.media_type,
                        digest.display_name,
                        digest.captured_at,
                        digest.content_hash,
                    ),
                )
            else:
                digest = self._decode_digest(conn, row)
            existing = conn.execute(
                "SELECT artifact_id FROM artifact_locations "
                "WHERE source_kind = ? AND source_ref = ? AND locator_hash = ?",
                (source_kind, public_ref, locator_hash),
            ).fetchone()
            if existing is not None:
                if existing["artifact_id"] == digest.artifact_id:
                    # Idempotent replay of an identical registration.
                    return digest
                raise ArtifactSourceConflict(
                    f"source {source_kind}:{public_ref} already links this "
                    f"locator to artifact {existing['artifact_id']} with "
                    "different content; a reused source identity never "
                    "rebinds silently"
                )
            location_body = {
                "artifact_id": digest.artifact_id,
                "source_kind": source_kind,
                "source_ref": public_ref,
                "locator_hash": locator_hash,
            }
            location_id = "loc_" + hash_hex(
                canonical_content_hash(location_body)
            )
            conn.execute(
                _INSERT_LOCATION_SQL,
                (
                    location_id,
                    digest.artifact_id,
                    source_kind,
                    public_ref,
                    locator_json,
                    locator_hash,
                    captured_at,
                ),
            )
            return digest

        return self._db._execute_write(_do)

    # ── Reads ──

    @staticmethod
    def _decode_digest(
        conn: "sqlite3.Connection", row: "sqlite3.Row"
    ) -> ArtifactDigest:
        first_location = conn.execute(
            "SELECT source_kind, source_ref FROM artifact_locations "
            "WHERE artifact_id = ? ORDER BY created_at, location_id LIMIT 1",
            (row["artifact_id"],),
        ).fetchone()
        if first_location is None:
            raise ArtifactIntegrityError(
                f"artifact {row['artifact_id']!r} has no source location"
            )
        rebuilt = build_artifact_digest(
            source_kind=first_location["source_kind"],
            source_ref=first_location["source_ref"],
            display_name=row["display_name"],
            media_type=row["media_type"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            mtime_ns=None,
            captured_at=row["captured_at"],
        )
        if (
            rebuilt.artifact_id != row["artifact_id"]
            or rebuilt.content_hash != row["content_hash"]
        ):
            raise ArtifactIntegrityError(
                f"artifact {row['artifact_id']!r} fails canonical hash "
                "recomputation"
            )
        return rebuilt

    def get(self, artifact_id: str) -> ArtifactDigest | None:
        def _do(conn: "sqlite3.Connection") -> ArtifactDigest | None:
            row = conn.execute(
                "SELECT * FROM artifact_digests WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
            return None if row is None else self._decode_digest(conn, row)

        return self._db._execute_read(_do)

    def digest_count(self) -> int:
        return self._db._execute_read(
            lambda conn: conn.execute(
                "SELECT COUNT(*) FROM artifact_digests"
            ).fetchone()[0]
        )

    def location_count(self, artifact_id: str) -> int:
        return self._db._execute_read(
            lambda conn: conn.execute(
                "SELECT COUNT(*) FROM artifact_locations WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()[0]
        )

    # ── Read-only recheck ──

    def recheck(
        self,
        artifact_id: str,
        *,
        allowed_roots: tuple[Path, ...],
    ) -> tuple[ArtifactRecheckResult, ...]:
        """Re-verify every stored location of one artifact, read-only."""

        def _read(conn: "sqlite3.Connection"):
            digest_row = conn.execute(
                "SELECT * FROM artifact_digests WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
            if digest_row is None:
                return None, ()
            location_rows = conn.execute(
                "SELECT * FROM artifact_locations WHERE artifact_id = ? "
                "ORDER BY created_at, location_id",
                (artifact_id,),
            ).fetchall()
            return dict(digest_row), tuple(dict(row) for row in location_rows)

        digest_row, locations = self._db._execute_read(_read)
        if digest_row is None:
            raise ArtifactCatalogError(f"unknown artifact {artifact_id!r}")
        roots = _resolve_roots(tuple(allowed_roots))
        checked_at = _now()
        results = tuple(
            self._recheck_location(digest_row, location, roots, checked_at)
            for location in locations
        )
        if locations:
            self._db._execute_write(
                lambda conn: conn.executemany(
                    "UPDATE artifact_locations SET last_checked_at = ? "
                    "WHERE location_id = ?",
                    [(checked_at, loc["location_id"]) for loc in locations],
                )
            )
        return results

    def _recheck_location(
        self,
        digest_row: dict,
        location: dict,
        roots: tuple[Path, ...],
        checked_at: str,
    ) -> ArtifactRecheckResult:
        def _result(
            status: RecheckStatus,
            detail: str,
            observed_sha256: str | None = None,
            observed_size_bytes: int | None = None,
        ) -> ArtifactRecheckResult:
            return ArtifactRecheckResult(
                artifact_id=digest_row["artifact_id"],
                location_id=location["location_id"],
                source_kind=location["source_kind"],
                source_ref=location["source_ref"],
                status=status,
                detail=detail,
                observed_sha256=observed_sha256,
                observed_size_bytes=observed_size_bytes,
                checked_at=checked_at,
            )

        try:
            locator = json.loads(location["locator_json"])
        except ValueError:
            return _result("ambiguous", "stored locator is unreadable")
        if not isinstance(locator, dict) or locator.get("kind") != "file":
            return _result(
                "ambiguous",
                "inline byte registration has no durable location to recheck",
            )
        raw_path = str(locator.get("path") or "")
        if not raw_path:
            return _result("ambiguous", "stored locator has no path")
        given = _normalized(raw_path)
        if not roots or not _is_under(given, roots):
            return _result(
                "inaccessible",
                "stored location is outside the allowed recheck roots",
            )
        try:
            capture = _safe_capture(
                given,
                roots,
                max_bytes=max(self._max_bytes, int(digest_row["size_bytes"])),
            )
        except FileNotFoundError:
            return _result("missing", "artifact file no longer exists")
        except ArtifactBoundaryError as exc:
            return _result("inaccessible", str(exc))
        except ArtifactTypeError as exc:
            return _result(
                "changed", f"path no longer holds a regular file: {exc}"
            )
        except ArtifactSizeError as exc:
            return _result(
                "changed", f"artifact size no longer matches: {exc}"
            )
        except ArtifactAmbiguityError as exc:
            return _result("ambiguous", str(exc))
        except OSError as exc:
            return _result(
                "inaccessible",
                f"cannot read artifact: {type(exc).__name__}",
            )
        if (
            capture.sha256 != digest_row["sha256"]
            or capture.size_bytes != digest_row["size_bytes"]
        ):
            return _result(
                "changed",
                "artifact bytes differ from the recorded digest",
                observed_sha256=capture.sha256,
                observed_size_bytes=capture.size_bytes,
            )
        return _result(
            "unchanged",
            "artifact bytes match the recorded digest",
            observed_sha256=capture.sha256,
            observed_size_bytes=capture.size_bytes,
        )
