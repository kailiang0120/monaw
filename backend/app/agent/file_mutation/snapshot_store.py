"""Protected, crash-safe private snapshots for native file mutations."""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.agent.file_mutation.fingerprint import (
    FingerprintError,
    canonical_path,
    fingerprint_path,
)
from app.agent.runtime_paths import RUNTIME_DIR

SNAPSHOT_VERSION = 1
SNAPSHOT_MAX_AGE_DAYS = 30
MAX_BEFORE_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BEFORE_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_PLAINTEXT_BYTES = 12 * 1024 * 1024
MAX_SNAPSHOT_STORED_BYTES = 16 * 1024 * 1024
MAX_SNAPSHOT_STORE_BYTES = 512 * 1024 * 1024
_SNAPSHOT_REF_RE = re.compile(r"^snap-v1-[0-9a-f]{32}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_DPAPI_MAGIC = b"MONAW-FILE-SNAPSHOT-DPAPI-1\n"
_lock = threading.RLock()


class SnapshotStoreError(RuntimeError):
    """A snapshot cannot be safely captured, published, or loaded."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _protect(data: bytes) -> bytes:
    if os.name != "nt":
        return data
    try:
        import win32crypt

        return _DPAPI_MAGIC + win32crypt.CryptProtectData(
            data, "Monaw file mutation snapshot", None, None, None, 0
        )
    except Exception as exc:
        raise SnapshotStoreError("Windows snapshot protection is unavailable") from exc


def _unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        return data
    if not data.startswith(_DPAPI_MAGIC):
        raise SnapshotStoreError("snapshot protection marker is invalid")
    try:
        import win32crypt

        return win32crypt.CryptUnprotectData(data[len(_DPAPI_MAGIC) :], None, None, None, 0)[1]
    except Exception as exc:
        raise SnapshotStoreError("Windows snapshot could not be authenticated") from exc


def _id_pair(value: object, *, field: str) -> list[int] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise SnapshotStoreError(f"snapshot {field} is invalid")
    if any(isinstance(part, bool) or not isinstance(part, int) or part < 0 for part in value):
        raise SnapshotStoreError(f"snapshot {field} is invalid")
    if int(value[1]) == 0:
        raise SnapshotStoreError(f"snapshot {field} is unavailable")
    return [int(value[0]), int(value[1])]


def _state(value: object, *, name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise SnapshotStoreError(f"snapshot {name} state is invalid")
    path = value.get("path")
    exists = value.get("exists")
    kind = value.get("kind")
    if not isinstance(path, str) or not path or isinstance(exists, bool) is False:
        raise SnapshotStoreError(f"snapshot {name} state is invalid")
    if kind not in {"file", "absent"}:
        raise SnapshotStoreError(f"snapshot {name} file type is unsupported")
    size = value.get("size")
    if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
        raise SnapshotStoreError(f"snapshot {name} size is invalid")
    sha = value.get("sha256")
    if sha is not None and (not isinstance(sha, str) or not _HASH_RE.fullmatch(sha)):
        raise SnapshotStoreError(f"snapshot {name} digest is invalid")
    identity = _id_pair(value.get("identity"), field=f"{name} identity")
    parent_identity = _id_pair(value.get("parent_identity"), field=f"{name} parent identity")
    modified_ns = value.get("modified_ns")
    created_ns = value.get("created_ns")
    for field_name, item in (("modified_ns", modified_ns), ("created_ns", created_ns)):
        if item is not None and (isinstance(item, bool) or not isinstance(item, int) or item < 0):
            raise SnapshotStoreError(f"snapshot {name} {field_name} is invalid")
    if exists:
        if kind != "file" or size is None or sha is None or parent_identity is None:
            raise SnapshotStoreError(f"snapshot {name} file state lacks verification metadata")
    elif kind != "absent" or size not in (None, 0) or identity is not None or parent_identity is None:
        raise SnapshotStoreError(f"snapshot {name} absent state lacks parent identity")
    return {
        "path": path,
        "exists": exists,
        "kind": kind,
        "size": size,
        "sha256": sha,
        "identity": identity,
        "parent_identity": parent_identity,
        "modified_ns": modified_ns,
        "created_ns": created_ns,
    }


def _lexical_is_under(path: str, root: str) -> bool:
    try:
        return Path(path).is_relative_to(Path(root))
    except (OSError, RuntimeError, ValueError):
        return False


def _operation_kind(operation: object) -> str:
    value = str(operation or "").strip().lower()
    if value in {"write", "write_file", "file_write", "append", "patch"}:
        return "file_write"
    if value in {"move", "move_file", "file_move", "rename", "rename_file"}:
        return "file_move"
    if value in {"copy", "copy_file", "file_copy"}:
        return "file_copy"
    if value in {"delete", "delete_file", "file_delete", "unlink"}:
        return "file_delete"
    raise SnapshotStoreError("operation is not supported for verified file recovery")


def _needs_before_image(operation_kind: str, operand: Mapping[str, object]) -> bool:
    before = operand.get("before")
    if not isinstance(before, Mapping) or before.get("exists") is not True:
        return False
    role = str(operand.get("role") or "").lower()
    if operation_kind in {"file_write", "file_delete"}:
        return True
    return operation_kind in {"file_move", "file_copy"} and role in {"destination", "dest", "target"}


def _validate_operation_operands(operation_kind: str, operands: list[dict[str, Any]]) -> None:
    """Require a fully specified, regular-file pre/postcondition shape."""
    if operation_kind == "file_write":
        if len(operands) != 1:
            raise SnapshotStoreError("write plan must identify one regular file")
        item = operands[0]
        before, after = item["before"], item["after"]
        strategy = item["identity_strategy"]
        if not after["exists"] or after["kind"] != "file" or after["parent_identity"] != before["parent_identity"]:
            raise SnapshotStoreError("write plan lacks a regular-file after-state")
        if strategy not in {"same", "replaced"}:
            raise SnapshotStoreError("write plan identity strategy is unsupported")
        if strategy == "same" and (not before["exists"] or after["identity"] != before["identity"]):
            raise SnapshotStoreError("in-place write does not preserve its planned identity")
        if strategy == "replaced" and before["exists"] and after["identity"] is not None and after["identity"] == before["identity"]:
            raise SnapshotStoreError("replacement write does not change its planned identity")
        if strategy == "replaced" and not before["exists"] and after["identity"] is None:
            raise SnapshotStoreError("created write lacks an expected post-state identity")
        return

    if operation_kind == "file_move":
        sources = [item for item in operands if item["role"].lower() == "source"]
        destinations = [item for item in operands if item["role"].lower() in {"destination", "dest", "target"}]
        if len(operands) != 2 or len(sources) != 1 or len(destinations) != 1:
            raise SnapshotStoreError("move plan must identify one source and destination")
        source, destination = sources[0], destinations[0]
        source_before, source_after = source["before"], source["after"]
        destination_before, destination_after = destination["before"], destination["after"]
        source_identity = source_before["identity"]
        if (
            not source_before["exists"]
            or source_after["exists"]
            or source["identity_strategy"] != "absent"
            or not destination_after["exists"]
            or destination["identity_strategy"] != "replaced"
            or destination_after["identity"] != source_identity
            or (
                destination_before["identity"] is not None
                and destination_after["identity"] == destination_before["identity"]
            )
            or destination_after["sha256"] != source_before["sha256"]
            or destination_after["size"] != source_before["size"]
            or destination_after["parent_identity"] != destination_before["parent_identity"]
            or source_identity[0] != destination_before["parent_identity"][0]
        ):
            raise SnapshotStoreError("move plan does not prove a same-volume identity-preserving rename")
        return

    if operation_kind == "file_copy":
        sources = [item for item in operands if item["role"].lower() == "source"]
        destinations = [item for item in operands if item["role"].lower() in {"destination", "dest", "target"}]
        if len(operands) != 2 or len(sources) != 1 or len(destinations) != 1:
            raise SnapshotStoreError("copy plan must identify one source and destination")
        source, destination = sources[0], destinations[0]
        source_before, source_after = source["before"], source["after"]
        destination_before, destination_after = destination["before"], destination["after"]
        if (
            not source_before["exists"]
            or source["identity_strategy"] != "same"
            or source_after["identity"] != source_before["identity"]
            or source_after["sha256"] != source_before["sha256"]
            or source_after["size"] != source_before["size"]
            or source_after["parent_identity"] != source_before["parent_identity"]
            or not destination_after["exists"]
            or destination["identity_strategy"] != "replaced"
            or destination_after["identity"] is None
            or (destination_before["identity"] is not None and destination_after["identity"] == destination_before["identity"])
            or destination_after["sha256"] != source_before["sha256"]
            or destination_after["size"] != source_before["size"]
            or destination_after["parent_identity"] != destination_before["parent_identity"]
        ):
            raise SnapshotStoreError("copy plan lacks a verifiable regular-file destination")
        return

    if operation_kind == "file_delete":
        if (
            len(operands) != 1
            or not operands[0]["before"]["exists"]
            or operands[0]["after"]["exists"]
            or operands[0]["identity_strategy"] != "absent"
        ):
            raise SnapshotStoreError("delete plan must identify one existing regular file")
        return

    raise SnapshotStoreError("operation is unsupported")


def _safe_root_identity(path: Path) -> tuple[int, int]:
    current = Path(path.anchor)
    pieces = path.parts[1:]
    for piece in pieces:
        current = current / piece
        try:
            value = os.lstat(current)
        except OSError as exc:
            raise SnapshotStoreError("authorized scope root is unavailable") from exc
        if (
            not stat.S_ISDIR(value.st_mode)
            or int(getattr(value, "st_file_attributes", 0)) & _FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise SnapshotStoreError("authorized scope root is not a safe directory")
    try:
        value = os.lstat(path)
    except OSError as exc:
        raise SnapshotStoreError("authorized scope root is unavailable") from exc
    identity = (int(value.st_dev), int(value.st_ino))
    if identity[0] < 0 or identity[1] <= 0:
        raise SnapshotStoreError("authorized scope root identity is unavailable")
    return identity


def _ensure_private_root(root: Path) -> None:
    """Create/validate the store directory and protect its POSIX permissions."""
    root = canonical_path(root)
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current = current / part
        try:
            os.mkdir(current)
        except FileExistsError:
            pass
        except FileNotFoundError as exc:
            raise SnapshotStoreError("snapshot parent directory is unavailable") from exc
        try:
            value = os.lstat(current)
        except OSError as exc:
            raise SnapshotStoreError("snapshot directory cannot be inspected") from exc
        if (
            not stat.S_ISDIR(value.st_mode)
            or int(getattr(value, "st_file_attributes", 0)) & _FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise SnapshotStoreError("snapshot directory is not a regular directory")
    if os.name != "nt":
        try:
            root.chmod(0o700)
            mode = stat.S_IMODE(os.lstat(root).st_mode)
            if mode != 0o700:
                raise SnapshotStoreError("snapshot directory permissions are not private")
        except OSError as exc:
            raise SnapshotStoreError("snapshot directory permissions could not be protected") from exc


def _atomic_publish(path: Path, data: bytes) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = -1
    try:
        descriptor = os.open(temp, flags, 0o600)
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            offset += os.write(descriptor, view[offset : offset + 64 * 1024])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if os.name == "nt":
            # MOVEFILE_WRITE_THROUGH makes the exclusive same-directory rename
            # durable before capture returns. Omitting REPLACE_EXISTING keeps
            # publication exclusive if an attacker pre-creates the final name.
            import ctypes
            from ctypes import wintypes

            move_file_ex = ctypes.windll.kernel32.MoveFileExW
            move_file_ex.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
            move_file_ex.restype = wintypes.BOOL
            if not move_file_ex(str(temp), str(path), 0x00000008):
                raise ctypes.WinError()
        else:
            # Linking is an exclusive atomic publication: an existing final
            # name is never overwritten, including by a pre-created symlink.
            os.link(temp, path)
            temp.unlink()
            dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except Exception as exc:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temp.unlink()
        except OSError:
            pass
        if isinstance(exc, SnapshotStoreError):
            raise
        raise SnapshotStoreError("snapshot could not be published atomically") from exc


def _read_limited(path: Path, *, limit: int) -> bytes:
    try:
        before = os.lstat(path)
        if (
            not stat.S_ISREG(before.st_mode)
            or int(getattr(before, "st_file_attributes", 0)) & _FILE_ATTRIBUTE_REPARSE_POINT
            or int(getattr(before, "st_nlink", 1)) != 1
            or int(before.st_size) > limit
        ):
            raise SnapshotStoreError("snapshot file is not a safe bounded regular file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or int(getattr(opened, "st_nlink", 1)) != 1
                or (int(opened.st_dev), int(opened.st_ino)) != (int(before.st_dev), int(before.st_ino))
                or int(opened.st_size) != int(before.st_size)
            ):
                raise SnapshotStoreError("snapshot file changed while opening")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(64 * 1024, limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise SnapshotStoreError("snapshot file exceeds its size limit")
                chunks.append(chunk)
            after_fd = os.fstat(descriptor)
            after_path = os.lstat(path)
            if (
                not stat.S_ISREG(after_path.st_mode)
                or int(getattr(after_path, "st_nlink", 1)) != 1
                or int(getattr(after_path, "st_file_attributes", 0)) & _FILE_ATTRIBUTE_REPARSE_POINT
                or
                (int(after_fd.st_dev), int(after_fd.st_ino), int(after_fd.st_size))
                != (int(opened.st_dev), int(opened.st_ino), int(opened.st_size))
                or (int(after_path.st_dev), int(after_path.st_ino), int(after_path.st_size))
                != (int(before.st_dev), int(before.st_ino), int(before.st_size))
            ):
                raise SnapshotStoreError("snapshot file changed while reading")
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    except SnapshotStoreError:
        raise
    except OSError as exc:
        raise SnapshotStoreError("snapshot file cannot be read safely") from exc


@contextlib.contextmanager
def _interprocess_store_lock(root: Path, *, timeout_seconds: float = 3.0):
    """Serialize quota accounting/publication/cleanup across backend processes."""
    lock_path = root / ".store.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        value = os.fstat(descriptor)
        path_value = os.lstat(lock_path)
        if (
            not stat.S_ISREG(value.st_mode)
            or not stat.S_ISREG(path_value.st_mode)
            or int(getattr(value, "st_nlink", 1)) != 1
            or int(getattr(path_value, "st_nlink", 1)) != 1
            or int(getattr(path_value, "st_file_attributes", 0)) & _FILE_ATTRIBUTE_REPARSE_POINT
            or (int(value.st_dev), int(value.st_ino)) != (int(path_value.st_dev), int(path_value.st_ino))
        ):
            raise SnapshotStoreError("snapshot store lock is not a safe regular file")
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        if int(value.st_size) == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
    except SnapshotStoreError:
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass
        raise
    except OSError as exc:
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass
        raise SnapshotStoreError("snapshot store lock is unavailable") from exc

    locked = False
    deadline = time.monotonic() + timeout_seconds
    try:
        if os.name == "nt":
            import msvcrt

            while time.monotonic() < deadline:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    time.sleep(0.02)
        else:
            import fcntl

            while time.monotonic() < deadline:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError:
                    time.sleep(0.02)
        if not locked:
            raise SnapshotStoreError("snapshot store lock timed out")
        after_lock = os.lstat(lock_path)
        current_fd = os.fstat(descriptor)
        if (
            (int(after_lock.st_dev), int(after_lock.st_ino))
            != (int(current_fd.st_dev), int(current_fd.st_ino))
            or int(getattr(after_lock, "st_nlink", 1)) != 1
            or int(getattr(after_lock, "st_file_attributes", 0)) & _FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise SnapshotStoreError("snapshot store lock path changed")
        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(descriptor)


class SnapshotStore:
    """Persist only bounded before-bytes and private plan state.

    On Windows, the complete private manifest is DPAPI-protected for the
    current user. On POSIX, the store directory and files are owner-only.
    References are opaque generated identifiers; callers never pass paths to
    the Journal or public DTOs.
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        max_before_file_bytes: int = MAX_BEFORE_FILE_BYTES,
        max_total_before_bytes: int = MAX_TOTAL_BEFORE_BYTES,
        max_store_bytes: int = MAX_SNAPSHOT_STORE_BYTES,
    ) -> None:
        self.root = Path(root) if root is not None else RUNTIME_DIR / "file_mutation" / "snapshots"
        if (
            isinstance(max_before_file_bytes, bool)
            or not isinstance(max_before_file_bytes, int)
            or max_before_file_bytes < 0
            or isinstance(max_total_before_bytes, bool)
            or not isinstance(max_total_before_bytes, int)
            or max_total_before_bytes < 0
            or isinstance(max_store_bytes, bool)
            or not isinstance(max_store_bytes, int)
            or max_store_bytes < 0
        ):
            raise ValueError("snapshot quotas must be nonnegative integers")
        self.max_before_file_bytes = max_before_file_bytes
        self.max_total_before_bytes = max_total_before_bytes
        self.max_store_bytes = max_store_bytes

    def _root(self) -> Path:
        _ensure_private_root(self.root)
        return canonical_path(self.root)

    def capture(
        self,
        plan: object,
        before_bytes_by_path: Mapping[str | os.PathLike[str], bytes] | None,
        *,
        run_id: str,
        operation_id: str,
        authorized_roots: Sequence[str | os.PathLike[str]],
    ) -> str:
        """Validate plan states, securely publish an opaque snapshot, then return its ref."""
        if not _SAFE_ID_RE.fullmatch(str(run_id or "")) or not _SAFE_ID_RE.fullmatch(str(operation_id or "")):
            raise SnapshotStoreError("snapshot binding identifiers are invalid")
        manifest_builder = getattr(plan, "private_manifest", None)
        if not callable(manifest_builder):
            raise SnapshotStoreError("mutation plan does not expose a private manifest")
        try:
            raw_plan = manifest_builder()
        except Exception as exc:
            raise SnapshotStoreError("mutation plan manifest is unavailable") from exc
        if not isinstance(raw_plan, Mapping) or raw_plan.get("version") != 1:
            raise SnapshotStoreError("mutation plan manifest version is unsupported")
        operation_kind = _operation_kind(raw_plan.get("operation"))
        binding_digest = raw_plan.get("binding_digest")
        if not isinstance(binding_digest, str) or not _HASH_RE.fullmatch(binding_digest):
            raise SnapshotStoreError("mutation binding digest is invalid")
        raw_operands = raw_plan.get("operands")
        if not isinstance(raw_operands, list) or not raw_operands or len(raw_operands) > 8:
            raise SnapshotStoreError("mutation plan operands are invalid")
        roots: list[tuple[Path, str]] = []
        if isinstance(authorized_roots, (str, bytes)) or not isinstance(authorized_roots, Sequence):
            raise SnapshotStoreError("authorized read scope is invalid")
        for raw_root in authorized_roots:
            root_path = canonical_path(raw_root)
            roots.append((root_path, str(root_path)))
        if not roots:
            raise SnapshotStoreError("no read-authorized scope was supplied")

        provided_bytes: dict[str, bytes] = {}
        for raw_path, data in (before_bytes_by_path or {}).items():
            key = str(canonical_path(raw_path))
            if not isinstance(data, bytes):
                raise SnapshotStoreError("before-image must be bytes")
            provided_bytes[key] = data

        authorized_operands: list[dict[str, object]] = []
        required_before_paths: set[str] = set()
        seen_paths: set[str] = set()
        total_before_bytes = 0
        captured_before: dict[str, str] = {}
        for raw_operand in raw_operands:
            if not isinstance(raw_operand, Mapping):
                raise SnapshotStoreError("mutation plan operand is invalid")
            path_value = raw_operand.get("path")
            role = raw_operand.get("role")
            strategy = raw_operand.get("identity_strategy")
            if not isinstance(path_value, str) or not isinstance(role, str) or strategy not in {"same", "replaced", "absent", "unknown"}:
                raise SnapshotStoreError("mutation plan operand is invalid")
            path = canonical_path(path_value)
            canonical = str(path)
            if canonical != path_value or canonical in seen_paths:
                raise SnapshotStoreError("mutation plan operand path is not canonical")
            seen_paths.add(canonical)
            before = _state(raw_operand.get("before"), name="before")
            after = _state(raw_operand.get("after"), name="after")
            if before["path"] != canonical or after["path"] != canonical:
                raise SnapshotStoreError("mutation plan state path does not match its operand")
            if before["exists"] and before["identity"] is None:
                raise SnapshotStoreError("before-state file identity is unavailable")
            if after["exists"] and strategy == "same" and after["identity"] is None:
                raise SnapshotStoreError("same-identity after-state lacks its expected identity")
            if after["exists"] and strategy == "replaced" and after["identity"] is not None and before["identity"] == after["identity"]:
                raise SnapshotStoreError("replacement identity must differ from its before-state")
            matching_roots = [item for item in roots if path == item[0] or path.is_relative_to(item[0])]
            if not matching_roots:
                raise SnapshotStoreError("operand is outside the authorized read scope")
            scope_path, scope_text = max(matching_roots, key=lambda item: len(item[1]))
            if scope_path == path:
                scope_kind = "exact_path"
                scope_identity = None
            else:
                scope_kind = "directory"
                scope_identity = list(_safe_root_identity(scope_path))

            try:
                observed = fingerprint_path(
                    path,
                    include_bytes=_needs_before_image(operation_kind, raw_operand),
                    max_bytes=self.max_before_file_bytes if _needs_before_image(operation_kind, raw_operand) else 8 * 1024 * 1024,
                )
            except FingerprintError as exc:
                raise SnapshotStoreError("operand cannot be safely fingerprinted") from exc
            if observed.path != canonical or observed.exists != before["exists"]:
                raise SnapshotStoreError("operand state changed after planning")
            if list(observed.parent_identity) != before["parent_identity"]:
                raise SnapshotStoreError("operand parent identity changed after planning")
            if observed.exists:
                if (
                    observed.kind != before["kind"]
                    or observed.size != before["size"]
                    or observed.sha256 != before["sha256"]
                    or list(observed.identity or ()) != before["identity"]
                    or (
                        before["modified_ns"] is not None
                        and observed.modified_ns != before["modified_ns"]
                    )
                    or (
                        os.name == "nt"
                        and before["created_ns"] is not None
                        and observed.created_ns != before["created_ns"]
                    )
                ):
                    raise SnapshotStoreError("operand fingerprint changed after planning")
            elif observed.kind != "absent":
                raise SnapshotStoreError("absent operand state changed after planning")

            if _needs_before_image(operation_kind, raw_operand):
                required_before_paths.add(canonical)
                supplied = provided_bytes.get(canonical)
                if supplied is None or observed.data is None or supplied != observed.data:
                    raise SnapshotStoreError("operation has no matching bounded before-image")
                if len(supplied) > self.max_before_file_bytes:
                    raise SnapshotStoreError("before-image exceeds its per-file quota")
                total_before_bytes += len(supplied)
                if total_before_bytes > self.max_total_before_bytes:
                    raise SnapshotStoreError("before-images exceed their total quota")
                captured_before[canonical] = base64.b64encode(supplied).decode("ascii")

            authorized_operands.append(
                {
                    "path": canonical,
                    "role": role,
                    "identity_strategy": strategy,
                    "before": before,
                    "after": after,
                    "authorized_root": scope_text,
                    "authorized_root_kind": scope_kind,
                    "authorized_root_identity": scope_identity,
                }
            )

        if set(captured_before) != required_before_paths:
            raise SnapshotStoreError("before-image set is incomplete")
        _validate_operation_operands(operation_kind, authorized_operands)
        created_at = _now()
        snapshot_ref = f"snap-v1-{uuid.uuid4().hex}"
        snapshot = {
            "version": SNAPSHOT_VERSION,
            "snapshot_ref": snapshot_ref,
            "run_id": run_id,
            "operation_id": operation_id,
            "operation_kind": operation_kind,
            "binding_digest": binding_digest,
            "created_at": created_at.isoformat(timespec="microseconds"),
            "expires_at": (created_at + timedelta(days=SNAPSHOT_MAX_AGE_DAYS)).isoformat(timespec="microseconds"),
            "operands": authorized_operands,
            "before_bytes": captured_before,
        }
        try:
            plaintext = json.dumps(snapshot, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise SnapshotStoreError("snapshot metadata is not serializable") from exc
        if len(plaintext) > MAX_SNAPSHOT_PLAINTEXT_BYTES:
            raise SnapshotStoreError("snapshot metadata exceeds its size limit")
        protected = _protect(plaintext)
        if len(protected) > MAX_SNAPSHOT_STORED_BYTES:
            raise SnapshotStoreError("protected snapshot exceeds its size limit")

        with _lock:
            root = self._root()
            with _interprocess_store_lock(root):
                existing_total = 0
                for path in root.glob("snap-v1-*.snap"):
                    try:
                        value = os.lstat(path)
                        if (
                            stat.S_ISREG(value.st_mode)
                            and not (int(getattr(value, "st_file_attributes", 0)) & _FILE_ATTRIBUTE_REPARSE_POINT)
                            and int(getattr(value, "st_nlink", 1)) == 1
                        ):
                            existing_total += int(value.st_size)
                    except OSError:
                        continue
                if existing_total + len(protected) > self.max_store_bytes:
                    raise SnapshotStoreError("snapshot store total quota is exhausted")
                _atomic_publish(root / f"{snapshot_ref}.snap", protected)
        return snapshot_ref

    def load(
        self,
        snapshot_ref: str,
        *,
        run_id: str | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Load and validate a private manifest from a generated opaque ref."""
        if not isinstance(snapshot_ref, str) or not _SNAPSHOT_REF_RE.fullmatch(snapshot_ref):
            raise SnapshotStoreError("snapshot reference is invalid")
        root = self._root()
        stored = _read_limited(root / f"{snapshot_ref}.snap", limit=MAX_SNAPSHOT_STORED_BYTES)
        plaintext = _unprotect(stored)
        if len(plaintext) > MAX_SNAPSHOT_PLAINTEXT_BYTES:
            raise SnapshotStoreError("snapshot metadata exceeds its size limit")
        try:
            snapshot = json.loads(plaintext.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SnapshotStoreError("snapshot metadata is invalid") from exc
        if not isinstance(snapshot, dict):
            raise SnapshotStoreError("snapshot metadata is invalid")
        if (
            snapshot.get("version") != SNAPSHOT_VERSION
            or snapshot.get("snapshot_ref") != snapshot_ref
            or not _SAFE_ID_RE.fullmatch(str(snapshot.get("run_id") or ""))
            or not _SAFE_ID_RE.fullmatch(str(snapshot.get("operation_id") or ""))
            or snapshot.get("operation_kind") not in {"file_write", "file_move", "file_copy", "file_delete"}
            or not isinstance(snapshot.get("binding_digest"), str)
            or not _HASH_RE.fullmatch(snapshot["binding_digest"])
        ):
            raise SnapshotStoreError("snapshot binding is invalid")
        if run_id is not None and snapshot["run_id"] != run_id:
            raise SnapshotStoreError("snapshot run binding does not match")
        if operation_id is not None and snapshot["operation_id"] != operation_id:
            raise SnapshotStoreError("snapshot operation binding does not match")
        created_at = _parse_datetime(snapshot.get("created_at"))
        expires_at = _parse_datetime(snapshot.get("expires_at"))
        if created_at is None or expires_at is None or expires_at < created_at:
            raise SnapshotStoreError("snapshot lifetime is invalid")
        operands = snapshot.get("operands")
        if not isinstance(operands, list) or not operands or len(operands) > 8:
            raise SnapshotStoreError("snapshot operands are invalid")
        seen: set[str] = set()
        for operand in operands:
            if not isinstance(operand, dict):
                raise SnapshotStoreError("snapshot operand is invalid")
            path_value = operand.get("path")
            if not isinstance(path_value, str) or str(canonical_path(path_value)) != path_value or path_value in seen:
                raise SnapshotStoreError("snapshot operand path is invalid")
            seen.add(path_value)
            before = _state(operand.get("before"), name="before")
            after = _state(operand.get("after"), name="after")
            strategy = operand.get("identity_strategy")
            if strategy not in {"same", "replaced", "absent", "unknown"}:
                raise SnapshotStoreError("snapshot identity strategy is invalid")
            if before["exists"] and before["identity"] is None:
                raise SnapshotStoreError("snapshot before-state identity is unavailable")
            if after["exists"] and strategy == "same" and after["identity"] is None:
                raise SnapshotStoreError("snapshot expected identity is unavailable")
            if before["path"] != path_value or after["path"] != path_value:
                raise SnapshotStoreError("snapshot state path is invalid")
            root_value = operand.get("authorized_root")
            root_kind = operand.get("authorized_root_kind")
            if not isinstance(root_value, str) or str(canonical_path(root_value)) != root_value:
                raise SnapshotStoreError("snapshot authorized root is invalid")
            if not _lexical_is_under(path_value, root_value):
                raise SnapshotStoreError("snapshot operand escaped its authorized scope")
            if root_kind not in {"directory", "exact_path"}:
                raise SnapshotStoreError("snapshot authorized root kind is invalid")
            if root_kind == "exact_path" and root_value != path_value:
                raise SnapshotStoreError("snapshot exact-path scope is invalid")
            if root_kind == "directory":
                _id_pair(operand.get("authorized_root_identity"), field="authorized root identity")
            elif operand.get("authorized_root_identity") is not None:
                raise SnapshotStoreError("snapshot exact-path scope identity is invalid")
        before_bytes = snapshot.get("before_bytes")
        if not isinstance(before_bytes, dict) or len(before_bytes) > 8:
            raise SnapshotStoreError("snapshot before-images are invalid")
        total_bytes = 0
        for path_value, encoded in before_bytes.items():
            if path_value not in seen or not isinstance(encoded, str) or len(encoded) > ((self.max_before_file_bytes + 2) // 3) * 4:
                raise SnapshotStoreError("snapshot before-image reference is invalid")
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise SnapshotStoreError("snapshot before-image encoding is invalid") from exc
            if len(raw) > self.max_before_file_bytes:
                raise SnapshotStoreError("snapshot before-image exceeds its per-file quota")
            total_bytes += len(raw)
            if total_bytes > self.max_total_before_bytes:
                raise SnapshotStoreError("snapshot before-images exceed their total quota")
            before = next(item["before"] for item in operands if item["path"] == path_value)
            import hashlib

            if not before["exists"] or hashlib.sha256(raw).hexdigest() != before["sha256"]:
                raise SnapshotStoreError("snapshot before-image digest does not match")
        required = {
            item["path"]
            for item in operands
            if _needs_before_image(snapshot["operation_kind"], item)
        }
        if required != set(before_bytes):
            raise SnapshotStoreError("snapshot operation before-images are incomplete")
        _validate_operation_operands(snapshot["operation_kind"], operands)
        return snapshot

    def before_bytes(self, snapshot: Mapping[str, Any], path: str | os.PathLike[str]) -> bytes | None:
        """Return one verified stored before-image for a private Undo consumer."""
        canonical = str(canonical_path(path))
        raw_images = snapshot.get("before_bytes")
        if not isinstance(raw_images, Mapping):
            raise SnapshotStoreError("snapshot before-images are invalid")
        encoded = raw_images.get(canonical)
        if encoded is None:
            return None
        try:
            data = base64.b64decode(str(encoded), validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise SnapshotStoreError("snapshot before-image encoding is invalid") from exc
        return data

    def cleanup(
        self,
        referenced_refs: set[str] | frozenset[str],
        *,
        now: datetime | None = None,
        orphan_grace_seconds: int = SNAPSHOT_MAX_AGE_DAYS * 24 * 60 * 60,
    ) -> dict[str, int]:
        """Remove only old unreferenced files; unresolved/recoverable refs survive expiry."""
        if not isinstance(referenced_refs, (set, frozenset)):
            raise ValueError("referenced_refs must be a set of opaque snapshot references")
        safe_refs = {ref for ref in referenced_refs if isinstance(ref, str) and _SNAPSHOT_REF_RE.fullmatch(ref)}
        if isinstance(orphan_grace_seconds, bool) or not isinstance(orphan_grace_seconds, int) or orphan_grace_seconds < 0:
            raise ValueError("orphan_grace_seconds must be a nonnegative integer")
        grace = min(SNAPSHOT_MAX_AGE_DAYS * 24 * 60 * 60, max(60, orphan_grace_seconds))
        cutoff = (now or _now()).timestamp() - grace
        removed = 0
        bytes_removed = 0
        root = self._root()
        with _lock, _interprocess_store_lock(root):
            for path in root.iterdir():
                if path.name.endswith(".tmp"):
                    try:
                        value = os.lstat(path)
                        if (
                            stat.S_ISREG(value.st_mode)
                            and int(getattr(value, "st_nlink", 1)) == 1
                            and not (int(getattr(value, "st_file_attributes", 0)) & _FILE_ATTRIBUTE_REPARSE_POINT)
                            and float(value.st_mtime) <= cutoff
                        ):
                            size = int(value.st_size)
                            path.unlink()
                            removed += 1
                            bytes_removed += size
                    except OSError:
                        continue
                    continue
                match = re.fullmatch(r"(snap-v1-[0-9a-f]{32})\.snap", path.name)
                if not match or match.group(1) in safe_refs:
                    continue
                try:
                    value = os.lstat(path)
                    if (
                        not stat.S_ISREG(value.st_mode)
                        or int(getattr(value, "st_nlink", 1)) != 1
                        or int(getattr(value, "st_file_attributes", 0)) & _FILE_ATTRIBUTE_REPARSE_POINT
                        or float(value.st_mtime) > cutoff
                    ):
                        continue
                    size = int(value.st_size)
                    path.unlink()
                    removed += 1
                    bytes_removed += size
                except OSError:
                    continue
        return {"snapshot_files_deleted": removed, "snapshot_bytes_deleted": bytes_removed}


_DEFAULT_STORE: SnapshotStore | None = None
_DEFAULT_STORE_LOCK = threading.Lock()


def get_snapshot_store() -> SnapshotStore:
    global _DEFAULT_STORE
    with _DEFAULT_STORE_LOCK:
        if _DEFAULT_STORE is None:
            _DEFAULT_STORE = SnapshotStore()
        return _DEFAULT_STORE
