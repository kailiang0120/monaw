"""Race-aware fingerprints for ordinary, single-link regular files."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

MAX_FINGERPRINT_BYTES = 8 * 1024 * 1024
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


class FingerprintError(OSError):
    """A path cannot be fingerprinted without following unsafe filesystem state."""


@dataclass(frozen=True)
class FileFingerprint:
    path: str
    exists: bool
    kind: str
    size: int
    sha256: str | None
    identity: tuple[int, int] | None
    parent_identity: tuple[int, int]
    device: int
    modified_ns: int | None
    created_ns: int | None
    data: bytes | None = field(default=None, repr=False, compare=False)


def _identity(value: os.stat_result) -> tuple[int, int]:
    device = int(value.st_dev)
    inode = int(value.st_ino)
    if device < 0 or inode <= 0:
        raise FingerprintError("filesystem identity is unavailable")
    return device, inode


def _is_reparse(value: os.stat_result) -> bool:
    return bool(int(getattr(value, "st_file_attributes", 0)) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _lstat_directory(path: Path) -> os.stat_result:
    try:
        value = os.lstat(path)
    except OSError as exc:
        raise FingerprintError("a parent directory is unavailable") from exc
    if _is_reparse(value) or not stat.S_ISDIR(value.st_mode):
        raise FingerprintError("a parent path is not a regular directory")
    _identity(value)
    return value


def _inspect_parent_chain(path: Path) -> os.stat_result:
    """Check every ancestor lexically, without resolving through reparse points."""
    parent = path.parent
    parts = parent.parts
    if not parts:
        raise FingerprintError("path has no parent directory")
    current = Path(parts[0])
    previous = _lstat_directory(current)
    for part in parts[1:]:
        current = current / part
        previous = _lstat_directory(current)
    return previous


def _created_ns(value: os.stat_result) -> int | None:
    # Windows Python 3.12 exposes birthtime explicitly. Its fstat ctime can
    # describe metadata changes while path stat/lstat retain the legacy
    # creation-time meaning; these two ctime values are not interchangeable.
    birth_time = getattr(value, "st_birthtime_ns", None)
    if birth_time is not None:
        return int(birth_time)
    if os.name == "nt":
        return int(getattr(value, "st_ctime_ns", 0))
    return None


def _windows_handle_identity(handle: int) -> tuple[int, int]:
    """Read a pinned handle using this Python's exact stat identity format.

    Python 3.12 Windows st_dev uses the full volume serial rather than the
    legacy 32-bit BY_HANDLE_FILE_INFORMATION volume field. Never truncate it
    when comparing native handles with fingerprints from os.stat/os.fstat.
    """
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.DuplicateHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    duplicate = wintypes.HANDLE()
    process = kernel32.GetCurrentProcess()
    if not kernel32.DuplicateHandle(process, handle, process, ctypes.byref(duplicate), 0, False, 2):
        raise FingerprintError("file handle identity could not be inspected")
    descriptor = -1
    try:
        descriptor = msvcrt.open_osfhandle(int(duplicate.value), os.O_RDONLY | getattr(os, "O_BINARY", 0))
        return _identity(os.fstat(descriptor))
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        else:
            kernel32.CloseHandle(duplicate)


def canonical_path(path: str | os.PathLike[str]) -> Path:
    """Return an absolute lexical path; deliberately do not resolve symlinks."""
    try:
        raw = os.fspath(path)
    except TypeError as exc:
        raise FingerprintError("path is invalid") from exc
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise FingerprintError("path is invalid")
    try:
        return Path(os.path.abspath(raw))
    except (OSError, RuntimeError, ValueError) as exc:
        raise FingerprintError("path is invalid") from exc


def fingerprint_path(
    path: str | os.PathLike[str],
    *,
    include_bytes: bool = False,
    max_bytes: int = MAX_FINGERPRINT_BYTES,
) -> FileFingerprint:
    """Fingerprint an absent leaf or a safe regular file and its parent identity.

    Symlinks/reparse points anywhere in the path, hard-linked files, special
    files, missing parents, oversized files, and files that change during the
    read are rejected. The caller must treat a rejected fingerprint as opaque.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a nonnegative integer")
    absolute = canonical_path(path)
    if absolute.name in {"", ".", ".."}:
        raise FingerprintError("path does not name a file")
    parent_stat = _inspect_parent_chain(absolute)
    parent_identity = _identity(parent_stat)
    try:
        initial = os.lstat(absolute)
    except FileNotFoundError:
        parent_after = _inspect_parent_chain(absolute)
        if _identity(parent_after) != parent_identity:
            raise FingerprintError("parent directory changed while checking absent path")
        try:
            os.lstat(absolute)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise FingerprintError("absent file path changed while being inspected") from exc
        else:
            raise FingerprintError("absent file appeared while being inspected")
        final_parent = _inspect_parent_chain(absolute)
        if _identity(final_parent) != parent_identity:
            raise FingerprintError("parent directory changed while checking absent path")
        return FileFingerprint(
            path=str(absolute),
            exists=False,
            kind="absent",
            size=0,
            sha256=None,
            identity=None,
            parent_identity=parent_identity,
            device=parent_identity[0],
            modified_ns=None,
            created_ns=None,
        )
    except OSError as exc:
        raise FingerprintError("file path cannot be inspected") from exc

    if _is_reparse(initial) or not stat.S_ISREG(initial.st_mode):
        raise FingerprintError("file is not a regular non-reparse file")
    if int(getattr(initial, "st_nlink", 1)) != 1:
        raise FingerprintError("hard-linked files are not supported")
    identity = _identity(initial)
    if identity[0] != parent_identity[0]:
        raise FingerprintError("cross-volume paths are not supported")
    size = int(initial.st_size)
    if size < 0 or size > max_bytes:
        raise FingerprintError("file exceeds the fingerprint size limit")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise FingerprintError("file cannot be opened safely") from exc
    chunks: list[bytes] = []
    digest = hashlib.sha256()
    total = 0
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse(opened)
            or int(getattr(opened, "st_nlink", 1)) != 1
            or _identity(opened) != identity
            or int(opened.st_size) != size
        ):
            raise FingerprintError("file changed before it could be read")
        while True:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise FingerprintError("file exceeds the fingerprint size limit")
            digest.update(chunk)
            if include_bytes:
                chunks.append(chunk)
        finished = os.fstat(descriptor)
        try:
            final_path_stat = os.lstat(absolute)
        except OSError as exc:
            raise FingerprintError("file path changed while being read") from exc
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        # The same descriptor API must retain its change-time check across
        # the read. Cross-API comparisons below use actual creation time.
        if any(getattr(opened, name, None) != getattr(finished, name, None)
               for name in (*stable_fields, "st_ctime_ns")) or _created_ns(opened) != _created_ns(finished):
            raise FingerprintError("file changed while being read")
        if (
            _is_reparse(final_path_stat)
            or not stat.S_ISREG(final_path_stat.st_mode)
            or int(getattr(final_path_stat, "st_nlink", 1)) != 1
            or _identity(final_path_stat) != identity
            or int(final_path_stat.st_size) != size
            or any(
                getattr(finished, name, None) != getattr(final_path_stat, name, None)
                for name in stable_fields
            )
            or _created_ns(finished) != _created_ns(final_path_stat)
            or (os.name != "nt" and getattr(finished, "st_ctime_ns", None) != getattr(final_path_stat, "st_ctime_ns", None))
            or total != size
        ):
            raise FingerprintError("file path changed while being read")
        # Recheck the parent identity after file IO so a replaced parent cannot
        # turn the recorded path into a different target.
        parent_after = _inspect_parent_chain(absolute)
        if _identity(parent_after) != parent_identity:
            raise FingerprintError("parent directory changed while file was read")
    finally:
        os.close(descriptor)

    return FileFingerprint(
        path=str(absolute),
        exists=True,
        kind="file",
        size=size,
        sha256=digest.hexdigest(),
        identity=identity,
        parent_identity=parent_identity,
        device=identity[0],
        modified_ns=int(getattr(initial, "st_mtime_ns", 0)),
        created_ns=_created_ns(initial),
        data=b"".join(chunks) if include_bytes else None,
    )
