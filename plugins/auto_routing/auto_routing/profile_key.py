"""Protected profile-local key for stable non-secret credential bindings."""

from __future__ import annotations

import os
import stat
import tempfile
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

from hermes_constants import get_config_path, get_hermes_home

from .config_io import profile_config_lock


CREDENTIAL_FINGERPRINT_KEY_BYTES = 32
CREDENTIAL_FINGERPRINT_KEY_NAME = "credential-selection.key"
PROFILE_CANARY_KEY_BYTES = 32
PROFILE_CANARY_KEY_NAME = "canary-assignment.key"


class ProfileKeyError(RuntimeError):
    """A profile credential-binding key is missing, unsafe, or corrupt."""


class _PinnedKeyParent:
    """Keep key publication anchored to one checked non-link directory."""

    def __init__(self, path: Path, home: Path, description: str) -> None:
        self.path = path
        self.home = home
        self.description = description
        self.descriptor: int | None = None
        self.windows_handle: int | None = None
        if os.name == "posix":
            flags = os.O_RDONLY
            flags |= getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            try:
                self.descriptor = os.open(str(path), flags)
            except OSError as error:
                raise ProfileKeyError(
                    f"{description} parent could not be pinned"
                ) from error
        else:
            self.windows_handle = _open_windows_directory_without_delete_share(
                path,
                description=description,
            )
        try:
            self.assert_current()
        except BaseException:
            self.close()
            raise

    def assert_current(self) -> None:
        """Require the pinned parent to remain the named in-profile directory."""
        if self.descriptor is not None:
            try:
                named = self.path.lstat()
            except FileNotFoundError as error:
                raise ProfileKeyError(f"{self.description} parent changed") from error
            opened = os.fstat(self.descriptor)
            if (
                not stat.S_ISDIR(named.st_mode)
                or stat.S_ISLNK(named.st_mode)
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            ):
                raise ProfileKeyError(f"{self.description} parent changed")
        _assert_profile_containment(
            self.path / ".containment-check",
            self.home,
            description=self.description,
        )

    def close(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None
        if self.windows_handle is not None:
            _close_windows_handle(self.windows_handle)
            self.windows_handle = None


def _open_windows_directory_without_delete_share(
    path: Path,
    *,
    description: str,
) -> int:
    """Pin a Windows directory and deny rename/delete while publishing."""
    import ctypes
    from ctypes import wintypes

    file_list_directory = 0x0001
    file_read_attributes = 0x0080
    file_share_read = 0x0001
    file_share_write = 0x0002
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    file_flag_open_reparse_point = 0x00200000
    file_attribute_directory = 0x0010
    file_attribute_reparse_point = 0x0400
    invalid_handle_value = ctypes.c_void_p(-1).value

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        file_list_directory | file_read_attributes,
        file_share_read | file_share_write,
        None,
        open_existing,
        file_flag_backup_semantics | file_flag_open_reparse_point,
        None,
    )
    if handle == invalid_handle_value:
        raise ProfileKeyError(
            f"{description} parent could not be pinned"
        ) from ctypes.WinError()
    information = _ByHandleFileInformation()
    get_information = ctypes.windll.kernel32.GetFileInformationByHandle
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    )
    get_information.restype = wintypes.BOOL
    if not get_information(handle, ctypes.byref(information)):
        error = ctypes.WinError()
        _close_windows_handle(int(handle))
        raise ProfileKeyError(
            f"{description} parent metadata could not be read"
        ) from error
    if (
        not information.dwFileAttributes & file_attribute_directory
        or information.dwFileAttributes & file_attribute_reparse_point
    ):
        _close_windows_handle(int(handle))
        raise ProfileKeyError(f"{description} parent must be a non-link directory")
    return int(handle)


def _close_windows_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.windll.kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    close_handle(handle)


@contextmanager
def _pinned_key_parent(
    path: Path,
    home: Path,
    *,
    description: str,
) -> Iterator[_PinnedKeyParent]:
    pinned = _PinnedKeyParent(path, home, description)
    try:
        yield pinned
    finally:
        pinned.close()


def credential_fingerprint_key_path(
    home: str | os.PathLike[str] | None = None,
) -> Path:
    root = Path(home) if home is not None else get_hermes_home()
    return root.expanduser().absolute() / "auto-routing" / CREDENTIAL_FINGERPRINT_KEY_NAME


def profile_canary_key_path(
    home: str | os.PathLike[str] | None = None,
) -> Path:
    root = Path(home) if home is not None else get_hermes_home()
    return root.expanduser().absolute() / "auto-routing" / PROFILE_CANARY_KEY_NAME


def _assert_profile_containment(
    path: Path,
    home: Path,
    *,
    description: str = "credential fingerprint key",
) -> None:
    resolved_home = home.expanduser().resolve()
    resolved_parent = path.parent.expanduser().resolve()
    if not resolved_parent.is_relative_to(resolved_home):
        raise ProfileKeyError(f"{description} escaped the active profile")


def _read_protected_key(
    path: Path,
    *,
    expected_bytes: int = CREDENTIAL_FINGERPRINT_KEY_BYTES,
    description: str = "credential fingerprint key",
) -> bytes:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ProfileKeyError(f"{description} must be a regular file")
    if os.name == "posix":
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ProfileKeyError(
                f"{description} must have owner-only permissions"
            )
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise ProfileKeyError(
                f"{description} must be owned by the active user"
            )
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(path), flags)
    except OSError as error:
        raise ProfileKeyError(f"{description} could not be opened") from error
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ProfileKeyError(f"{description} changed while opening")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            value = stream.read(expected_bytes + 1)
    finally:
        os.close(descriptor)
    if len(value) != expected_bytes:
        raise ProfileKeyError(f"{description} has invalid length")
    return value


def read_profile_credential_fingerprint_key_if_present(
    home: str | os.PathLike[str] | None = None,
) -> bytes | None:
    root = Path(home) if home is not None else get_hermes_home()
    path = credential_fingerprint_key_path(root)
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    _assert_profile_containment(path, root)
    return _read_protected_key(path)


def read_profile_canary_key(
    home: str | os.PathLike[str] | None = None,
) -> bytes:
    """Read a valid protected canary key, failing closed when it is absent."""
    root = Path(home) if home is not None else get_hermes_home()
    path = profile_canary_key_path(root)
    try:
        path.parent.lstat()
    except FileNotFoundError as error:
        raise ProfileKeyError("profile canary key is missing") from error
    try:
        # Hold the checked parent open through the whole read.  POSIX reads are
        # relative to the descriptor; Windows denies rename/delete on the
        # pinned directory and verifies containment before returning.
        with _pinned_key_parent(
            path.parent,
            root,
            description="profile canary key",
        ) as pinned:
            if pinned.descriptor is not None:
                value = _read_protected_key_relative(
                    pinned.descriptor,
                    path.name,
                    expected_bytes=PROFILE_CANARY_KEY_BYTES,
                    description="profile canary key",
                )
            else:
                value = _read_protected_key(
                    path,
                    expected_bytes=PROFILE_CANARY_KEY_BYTES,
                    description="profile canary key",
                )
            pinned.assert_current()
            return value
    except FileNotFoundError as error:
        raise ProfileKeyError("profile canary key is missing") from error


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _generate_profile_key(key_bytes: int) -> bytes:
    return os.urandom(key_bytes)


def _read_protected_key_relative(
    parent_descriptor: int,
    name: str,
    *,
    expected_bytes: int,
    description: str,
) -> bytes:
    metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ProfileKeyError(f"{description} must be a regular file")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ProfileKeyError(f"{description} must have owner-only permissions")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ProfileKeyError(f"{description} must be owned by the active user")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ProfileKeyError(f"{description} changed while opening")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            value = stream.read(expected_bytes + 1)
    finally:
        os.close(descriptor)
    if len(value) != expected_bytes:
        raise ProfileKeyError(f"{description} has invalid length")
    return value


def _read_concurrent_winner(
    pinned: _PinnedKeyParent,
    name: str,
    *,
    expected_bytes: int,
    description: str,
) -> bytes:
    """Read a concurrent winner only while its pinned parent remains current."""
    assert pinned.descriptor is not None
    value = _read_protected_key_relative(
        pinned.descriptor,
        name,
        expected_bytes=expected_bytes,
        description=description,
    )
    pinned.assert_current()
    return value


def _create_key_relative_to_pinned_parent(
    pinned: _PinnedKeyParent,
    path: Path,
    value: bytes,
    *,
    description: str,
) -> bytes:
    assert pinned.descriptor is not None
    temporary_name = f".{path.name}.{os.urandom(12).hex()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(temporary_name, flags, 0o600, dir_fd=pinned.descriptor)
    descriptor_owned = True
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "wb")
        descriptor_owned = False
        with stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        pinned.assert_current()
        try:
            os.link(
                temporary_name,
                path.name,
                src_dir_fd=pinned.descriptor,
                dst_dir_fd=pinned.descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            return _read_concurrent_winner(
                pinned,
                path.name,
                expected_bytes=len(value),
                description=description,
            )
        try:
            pinned.assert_current()
        except BaseException:
            try:
                os.unlink(path.name, dir_fd=pinned.descriptor)
                os.fsync(pinned.descriptor)
            except FileNotFoundError:
                pass
            raise
        os.fsync(pinned.descriptor)
        return value
    finally:
        if descriptor_owned:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.unlink(temporary_name, dir_fd=pinned.descriptor)
        except FileNotFoundError:
            pass


def _create_key_with_locked_windows_parent(
    pinned: _PinnedKeyParent,
    path: Path,
    value: bytes,
    *,
    description: str,
) -> bytes:
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary)
    descriptor_owned = True
    try:
        stream = os.fdopen(descriptor, "wb")
        descriptor_owned = False
        with stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        pinned.assert_current()
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            return _read_protected_key(
                path,
                expected_bytes=len(value),
                description=description,
            )
        return value
    finally:
        if descriptor_owned:
            try:
                os.close(descriptor)
            except OSError:
                pass
        temporary_path.unlink(missing_ok=True)


def _create_key_without_replacement(
    path: Path,
    *,
    home: Path,
    key_bytes: int = CREDENTIAL_FINGERPRINT_KEY_BYTES,
    description: str = "credential fingerprint key",
) -> bytes:
    with _pinned_key_parent(
        path.parent,
        home,
        description=description,
    ) as pinned:
        value = _generate_profile_key(key_bytes)
        pinned.assert_current()
        if pinned.descriptor is not None:
            return _create_key_relative_to_pinned_parent(
                pinned,
                path,
                value,
                description=description,
            )
        result = _create_key_with_locked_windows_parent(
            pinned,
            path,
            value,
            description=description,
        )
        _fsync_directory(path.parent)
        return result


def ensure_profile_credential_fingerprint_key(
    home: str | os.PathLike[str] | None = None,
    *,
    config_path: str | os.PathLike[str] | None = None,
) -> bytes:
    root = (Path(home) if home is not None else get_hermes_home()).expanduser().absolute()
    logical_config = (
        Path(config_path)
        if config_path is not None
        else Path(get_config_path())
    ).expanduser().absolute()
    if not logical_config.resolve(strict=False).is_relative_to(root.resolve()):
        raise ProfileKeyError("credential fingerprint key belongs to another profile")
    path = credential_fingerprint_key_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_profile_containment(path, root)
    with profile_config_lock(logical_config):
        existing = read_profile_credential_fingerprint_key_if_present(root)
        if existing is not None:
            return existing
        return _create_key_without_replacement(path, home=root)


def ensure_profile_canary_key(
    home: str | os.PathLike[str] | None = None,
    *,
    config_path: str | os.PathLike[str] | None = None,
) -> bytes:
    """Create once or read the active profile's protected canary key."""
    root = (Path(home) if home is not None else get_hermes_home()).expanduser().absolute()
    logical_config = (
        Path(config_path) if config_path is not None else Path(get_config_path())
    ).expanduser().absolute()
    if not logical_config.resolve(strict=False).is_relative_to(root.resolve()):
        raise ProfileKeyError("profile canary key belongs to another profile")
    path = profile_canary_key_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_profile_containment(path, root, description="profile canary key")
    with profile_config_lock(logical_config):
        try:
            return read_profile_canary_key(root)
        except ProfileKeyError as error:
            if not isinstance(error.__cause__, FileNotFoundError):
                raise
        return _create_key_without_replacement(
            path,
            home=root,
            key_bytes=PROFILE_CANARY_KEY_BYTES,
            description="profile canary key",
        )


__all__ = [
    "CREDENTIAL_FINGERPRINT_KEY_BYTES",
    "CREDENTIAL_FINGERPRINT_KEY_NAME",
    "PROFILE_CANARY_KEY_BYTES",
    "PROFILE_CANARY_KEY_NAME",
    "ProfileKeyError",
    "credential_fingerprint_key_path",
    "ensure_profile_credential_fingerprint_key",
    "ensure_profile_canary_key",
    "profile_canary_key_path",
    "read_profile_canary_key",
    "read_profile_credential_fingerprint_key_if_present",
]
