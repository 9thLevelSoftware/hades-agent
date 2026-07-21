"""Root-contained verification of local Ed25519 ranking packs."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, BinaryIO, Iterator

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .models import (
    DurableIdentifier,
    MAX_DECISION_CANDIDATES,
    RankingPackMetadata,
    RankingPackTrust,
    RuntimeStableId,
)


_MAX_RANKING_PACK_BYTES = 2 * 1024 * 1024
_PACK_ROOT_COMPONENTS = ("auto-routing", "ranking-packs")


if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _GENERIC_READ = 0x80000000
    _FILE_READ_ATTRIBUTES = 0x0080
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _create_file = _kernel32.CreateFileW
    _create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _create_file.restype = wintypes.HANDLE
    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = (wintypes.HANDLE,)
    _close_handle.restype = wintypes.BOOL
    _get_file_information = _kernel32.GetFileInformationByHandle
    _get_file_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    )
    _get_file_information.restype = wintypes.BOOL
    _get_final_path = _kernel32.GetFinalPathNameByHandleW
    _get_final_path.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    _get_final_path.restype = wintypes.DWORD


class RankingPackError(RuntimeError):
    """Content-free failure raised before an untrusted pack can be used."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class _RankingRowModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    quality: Annotated[
        float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)
    ]
    reliability: Annotated[
        float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)
    ]
    latency: Annotated[
        float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)
    ]
    cost: Annotated[
        float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)
    ]


class _RankingPackEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Annotated[int, Field(ge=1, le=1, strict=True)]
    pack_id: DurableIdentifier
    issued_at: datetime
    expires_at: datetime
    key_id: RuntimeStableId
    rankings: Annotated[
        dict[RuntimeStableId, _RankingRowModel],
        Field(min_length=1, max_length=MAX_DECISION_CANDIDATES),
    ]
    signature: Annotated[str, Field(min_length=1, max_length=128)]

    @model_validator(mode="after")
    def require_bounded_utc_validity_window(self) -> "_RankingPackEnvelope":
        if (
            self.issued_at.tzinfo is None
            or self.issued_at.utcoffset() is None
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
        ):
            raise ValueError("ranking-pack timestamps require UTC offsets")
        if self.expires_at <= self.issued_at:
            raise ValueError("ranking-pack expiry must follow issuance")
        return self


@dataclass(frozen=True, slots=True)
class RankingPackRow:
    """Normalized bounded metrics for one stable runtime identity."""

    runtime_id: str
    quality: float
    reliability: float
    latency: float
    cost: float


class VerifiedRankingPackMetadata(RankingPackMetadata):
    """Existing content-free metadata with a reader-friendly pack-id alias."""

    @property
    def pack_id(self) -> str:
        return self.ranking_pack_id


@dataclass(frozen=True, slots=True)
class VerifiedRankingPack:
    """Verified metadata and normalized lookup rows, never the raw envelope."""

    metadata: VerifiedRankingPackMetadata
    rankings: tuple[RankingPackRow, ...]

    @classmethod
    def from_envelope(
        cls,
        envelope: _RankingPackEnvelope,
        *,
        sha256: str,
        verified_at: datetime,
    ) -> "VerifiedRankingPack":
        rows = tuple(
            RankingPackRow(
                runtime_id=runtime_id,
                quality=float(score.quality),
                reliability=float(score.reliability),
                latency=float(score.latency),
                cost=float(score.cost),
            )
            for runtime_id, score in sorted(envelope.rankings.items())
        )
        return cls(
            metadata=VerifiedRankingPackMetadata(
                ranking_pack_id=envelope.pack_id,
                ranking_pack_sha256=sha256,
                schema_version=str(envelope.schema_version),
                verified_at=_iso(verified_at),
            ),
            rankings=rows,
        )

    def rank_for(self, runtime_id: str) -> RankingPackRow | None:
        for row in self.rankings:
            if row.runtime_id == runtime_id:
                return row
        return None


def _iso(value: datetime) -> str:
    normalized = _utc(value)
    return normalized.isoformat().replace("+00:00", "Z")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _canonical_signed_bytes(document: Mapping[str, object]) -> bytes:
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    return json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON key")
        document[key] = value
    return document


def _decode_public_key(encoded: str) -> tuple[Ed25519PublicKey, bytes]:
    try:
        key_bytes = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise RankingPackError("ranking_pack_trust_invalid") from error
    try:
        if len(key_bytes) == 32:
            return Ed25519PublicKey.from_public_bytes(key_bytes), key_bytes
        loaded = serialization.load_der_public_key(key_bytes)
    except (TypeError, ValueError) as error:
        raise RankingPackError("ranking_pack_trust_invalid") from error
    if not isinstance(loaded, Ed25519PublicKey):
        raise RankingPackError("ranking_pack_trust_invalid")
    return loaded, key_bytes


def _trusted_key(key_id: str, encoded_keys: tuple[str, ...]) -> Ed25519PublicKey:
    selected: Ed25519PublicKey | None = None
    for encoded in encoded_keys:
        public_key, configured_bytes = _decode_public_key(encoded)
        raw_bytes = public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        configured_ids = {
            hashlib.sha256(configured_bytes).hexdigest(),
            hashlib.sha256(raw_bytes).hexdigest(),
        }
        if key_id in configured_ids:
            selected = public_key
    if selected is None:
        raise RankingPackError("ranking_pack_key_untrusted")
    return selected


def ranking_trust_summary(trust: RankingPackTrust) -> dict[str, object]:
    """Return canonical public-key fingerprints without exposing key material."""
    fingerprints = []
    for encoded in trust.trusted_ed25519_public_keys:
        public_key, _configured_bytes = _decode_public_key(encoded)
        raw_bytes = public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        fingerprints.append(hashlib.sha256(raw_bytes).hexdigest())
    fingerprints.sort()
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprints,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "ranking_pack_path": trust.ranking_pack_path,
        "trusted_key_count": len(fingerprints),
        "trusted_key_set_fingerprint": fingerprint,
    }


def _pack_path_components(configured_path: str) -> tuple[str, ...]:
    normalized = configured_path.replace("\\", "/")
    components = tuple(normalized.split("/"))
    invalid = (
        not normalized
        or normalized.startswith("/")
        or len(components) <= len(_PACK_ROOT_COMPONENTS)
        or components[: len(_PACK_ROOT_COMPONENTS)] != _PACK_ROOT_COMPONENTS
        or any(
            not component
            or component in {".", ".."}
            or "\x00" in component
            or ":" in component
            or component.endswith((" ", "."))
            for component in components
        )
    )
    if invalid:
        raise RankingPackError("ranking_pack_outside_allowed_root")
    return components[len(_PACK_ROOT_COMPONENTS) :]


def _posix_directory_flags() -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required):
        raise RankingPackError("ranking_pack_platform_unsupported")
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


@contextmanager
def _open_posix_pack_stream(
    home: Path,
    relative_components: tuple[str, ...],
) -> Iterator[BinaryIO]:
    directory_flags = _posix_directory_flags()
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    with ExitStack() as stack:
        try:
            parent_fd = os.open(home, directory_flags)
            stack.callback(os.close, parent_fd)
            for component in _PACK_ROOT_COMPONENTS + relative_components[:-1]:
                parent_fd = os.open(
                    component,
                    directory_flags,
                    dir_fd=parent_fd,
                )
                stack.callback(os.close, parent_fd)
            file_fd = os.open(
                relative_components[-1],
                file_flags,
                dir_fd=parent_fd,
            )
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                os.close(file_fd)
                raise RankingPackError("ranking_pack_outside_allowed_root")
            try:
                stream = stack.enter_context(
                    os.fdopen(file_fd, "rb", closefd=True)
                )
            except BaseException:
                os.close(file_fd)
                raise
        except RankingPackError:
            raise
        except (OSError, TypeError, ValueError):
            raise RankingPackError("ranking_pack_outside_allowed_root") from None
        yield stream


def _windows_open_handle(path: Path, *, directory: bool) -> int:
    desired_access = _FILE_READ_ATTRIBUTES | (0 if directory else _GENERIC_READ)
    flags = _FILE_FLAG_OPEN_REPARSE_POINT
    if directory:
        flags |= _FILE_FLAG_BACKUP_SEMANTICS
    handle = _create_file(
        str(path),
        desired_access,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        flags,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error())
    return int(handle)


def _windows_close_handle(handle: int) -> None:
    _close_handle(wintypes.HANDLE(handle))


def _windows_handle_attributes(handle: int) -> int:
    information = _ByHandleFileInformation()
    if not _get_file_information(
        wintypes.HANDLE(handle),
        ctypes.byref(information),
    ):
        raise OSError(ctypes.get_last_error())
    return int(information.file_attributes)


def _windows_validate_handle(
    handle: int,
    *,
    directory: bool,
    expected_path: str | None = None,
) -> None:
    attributes = _windows_handle_attributes(handle)
    is_directory = bool(attributes & _FILE_ATTRIBUTE_DIRECTORY)
    if (
        attributes & _FILE_ATTRIBUTE_REPARSE_POINT
        or is_directory is not directory
    ):
        raise RankingPackError("ranking_pack_outside_allowed_root")
    if expected_path is not None and (
        _windows_final_path(handle).casefold() != expected_path.casefold()
    ):
        raise RankingPackError("ranking_pack_outside_allowed_root")


def _windows_final_path(handle: int) -> str:
    capacity = 32_768
    buffer = ctypes.create_unicode_buffer(capacity)
    length = _get_final_path(
        wintypes.HANDLE(handle),
        buffer,
        capacity,
        0,
    )
    if not length or length >= capacity:
        raise OSError(ctypes.get_last_error())
    return buffer.value.rstrip("\\/")


@contextmanager
def _open_windows_pack_stream(
    home: Path,
    relative_components: tuple[str, ...],
) -> Iterator[BinaryIO]:
    with ExitStack() as stack:
        current_path = Path(os.path.abspath(home))
        try:
            home_handle = _windows_open_handle(current_path, directory=True)
            stack.callback(_windows_close_handle, home_handle)
            _windows_validate_handle(home_handle, directory=True)
            home_path = _windows_final_path(home_handle)
            _windows_validate_handle(
                home_handle,
                directory=True,
                expected_path=home_path,
            )
            expected_path = home_path

            directory_components = (
                _PACK_ROOT_COMPONENTS + relative_components[:-1]
            )
            for component in directory_components:
                _windows_validate_handle(
                    home_handle,
                    directory=True,
                    expected_path=home_path,
                )
                current_path /= component
                expected_path = f"{expected_path}\\{component}"
                directory_handle = _windows_open_handle(
                    current_path,
                    directory=True,
                )
                stack.callback(_windows_close_handle, directory_handle)
                _windows_validate_handle(
                    directory_handle,
                    directory=True,
                    expected_path=expected_path,
                )
                _windows_validate_handle(
                    home_handle,
                    directory=True,
                    expected_path=home_path,
                )

            _windows_validate_handle(
                home_handle,
                directory=True,
                expected_path=home_path,
            )
            current_path /= relative_components[-1]
            expected_file_path = f"{expected_path}\\{relative_components[-1]}"
            file_handle = _windows_open_handle(current_path, directory=False)
            try:
                _windows_validate_handle(
                    file_handle,
                    directory=False,
                    expected_path=expected_file_path,
                )
                _windows_validate_handle(
                    home_handle,
                    directory=True,
                    expected_path=home_path,
                )
                file_fd = msvcrt.open_osfhandle(
                    file_handle,
                    os.O_RDONLY | getattr(os, "O_BINARY", 0),
                )
                file_handle = 0
            finally:
                if file_handle:
                    _windows_close_handle(file_handle)
            try:
                stream = stack.enter_context(
                    os.fdopen(file_fd, "rb", closefd=True)
                )
            except BaseException:
                os.close(file_fd)
                raise
        except RankingPackError:
            raise
        except (OSError, TypeError, ValueError):
            raise RankingPackError("ranking_pack_outside_allowed_root") from None
        yield stream


@contextmanager
def _open_pack_stream(
    home: Path,
    configured_path: str,
) -> Iterator[BinaryIO]:
    relative_components = _pack_path_components(configured_path)
    if os.name == "nt":
        with _open_windows_pack_stream(home, relative_components) as stream:
            yield stream
        return
    with _open_posix_pack_stream(home, relative_components) as stream:
        yield stream


def _read_bounded_bytes(stream: BinaryIO) -> bytes:
    try:
        pack_bytes = stream.read(_MAX_RANKING_PACK_BYTES + 1)
    except OSError:
        raise RankingPackError("ranking_pack_unreadable") from None
    if not pack_bytes or len(pack_bytes) > _MAX_RANKING_PACK_BYTES:
        raise RankingPackError("ranking_pack_malformed")
    return pack_bytes


def _read_document(stream: BinaryIO) -> tuple[dict[str, object], bytes]:
    pack_bytes = _read_bounded_bytes(stream)
    try:
        document = json.loads(
            pack_bytes.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise RankingPackError("ranking_pack_malformed") from error
    if not isinstance(document, dict):
        raise RankingPackError("ranking_pack_malformed")
    return document, pack_bytes


def load_verified_ranking_pack(
    *,
    home: Path,
    trust: RankingPackTrust,
    now: datetime,
) -> VerifiedRankingPack:
    """Read and authenticate one configured local ranking pack."""
    with _open_pack_stream(home, trust.ranking_pack_path) as stream:
        document, pack_bytes = _read_document(stream)
    try:
        envelope = _RankingPackEnvelope.model_validate(document)
    except ValidationError as error:
        raise RankingPackError("ranking_pack_malformed") from error

    current = _utc(now)
    if envelope.expires_at.astimezone(UTC) <= current:
        raise RankingPackError("ranking_pack_expired")
    if envelope.issued_at.astimezone(UTC) > current:
        raise RankingPackError("ranking_pack_not_yet_valid")

    public_key = _trusted_key(
        envelope.key_id,
        trust.trusted_ed25519_public_keys,
    )
    try:
        signature = base64.b64decode(envelope.signature, validate=True)
        if len(signature) != 64:
            raise ValueError("invalid Ed25519 signature length")
        public_key.verify(signature, _canonical_signed_bytes(document))
    except (InvalidSignature, TypeError, ValueError) as error:
        raise RankingPackError("ranking_pack_signature_invalid") from error

    return VerifiedRankingPack.from_envelope(
        envelope,
        sha256=hashlib.sha256(pack_bytes).hexdigest(),
        verified_at=current,
    )


def ranking_pack_status(
    *,
    home: Path,
    trust: RankingPackTrust,
    now: datetime,
) -> dict[str, object]:
    """Return only content-free verification status and pack fingerprints."""
    try:
        pack = load_verified_ranking_pack(home=home, trust=trust, now=now)
    except RankingPackError as error:
        return {"status": "invalid", "reason_code": error.reason_code}
    return {
        "status": "verified",
        "reason_code": None,
        **pack.metadata.model_dump(mode="json"),
        "ranking_count": len(pack.rankings),
    }


__all__ = [
    "RankingPackError",
    "RankingPackRow",
    "VerifiedRankingPack",
    "VerifiedRankingPackMetadata",
    "load_verified_ranking_pack",
    "ranking_trust_summary",
    "ranking_pack_status",
]
