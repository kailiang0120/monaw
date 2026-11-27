from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from app.agent.runtime_paths import RUNTIME_DIR
from app.agent.file_mutation.fingerprint import fingerprint_path

SANDBOX_RUNTIME_DIR = RUNTIME_DIR / "sandbox"
SANDBOX_RUNS_DIR = SANDBOX_RUNTIME_DIR / "runs"
SANDBOX_MANIFESTS_DIR = SANDBOX_RUNTIME_DIR / "manifests"
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
DEFAULT_MAX_COPY_OUT_BYTES = 104857600
MAX_WORKSPACE_ENTRIES = 10000


@dataclass(slots=True)
class SandboxPathPolicy:
    allowed_input_roots: list[Path] = field(default_factory=list)
    allowed_output_roots: list[Path] = field(default_factory=list)
    max_copy_out_bytes: int = DEFAULT_MAX_COPY_OUT_BYTES
    max_copy_in_bytes: int = 104857600
    initial_files: dict[str, str] = field(default_factory=dict)
    blocked_roots: list[Path] = field(default_factory=list)

    @classmethod
    def from_strings(
        cls,
        *,
        allowed_input_roots: Iterable[str] = (),
        allowed_output_roots: Iterable[str] = (),
        max_copy_out_bytes: int = DEFAULT_MAX_COPY_OUT_BYTES,
        max_copy_in_bytes: int = 104857600,
        initial_files: dict[str, str] | None = None,
        blocked_roots: Iterable[str] = (),
    ) -> "SandboxPathPolicy":
        return cls(
            allowed_input_roots=[canonical_path(root) for root in allowed_input_roots],
            allowed_output_roots=[canonical_path(root) for root in allowed_output_roots],
            max_copy_out_bytes=max_copy_out_bytes,
            max_copy_in_bytes=max_copy_in_bytes,
            initial_files=dict(initial_files or {}),
            blocked_roots=[canonical_path(root) for root in blocked_roots],
        )


def canonical_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def ensure_allowed_path(path: str | Path, roots: Iterable[Path], *, purpose: str) -> Path:
    canonical = canonical_path(path)
    canonical_roots = [canonical_path(root) for root in roots]
    if not canonical_roots:
        raise PermissionError(f"No sandbox {purpose} roots are configured")
    if not any(is_relative_to(canonical, root) or canonical == root for root in canonical_roots):
        raise PermissionError(f"Path is outside allowed sandbox {purpose} roots: {canonical}")
    return canonical


def ensure_plain_path(path: Path, root: Path) -> None:
    """Reject links, Windows reparse points, and special files without resolving them."""
    relative = path.relative_to(root)
    current = root
    for part in (None, *relative.parts):
        if part is not None:
            current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if (
            stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
            or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)
        ):
            raise PermissionError(f"Unsafe sandbox path: {current}")
        if current != path and not stat.S_ISDIR(info.st_mode):
            raise PermissionError(f"Sandbox parent is not a directory: {current}")


def is_blocked_path(path: Path, blocked_roots: Iterable[Path]) -> bool:
    """Check protected descendants as well as a protected workspace root."""
    return any(path == root or is_relative_to(path, root) for root in blocked_roots)


def regular_workspace_files(root: Path, *, skip_unsafe: bool = False):
    """Walk ordinary files without traversing symlinks or Windows junctions."""
    ensure_plain_path(root, root)
    entries = 0
    for directory, names, files in os.walk(root, followlinks=False):
        entries += len(names) + len(files)
        if entries > MAX_WORKSPACE_ENTRIES:
            raise PermissionError("Sandbox workspace entry limit exceeded")
        parent = Path(directory)
        ensure_plain_path(parent, root)
        for name in list(names):
            try:
                ensure_plain_path(parent / name, root)
            except PermissionError:
                if not skip_unsafe:
                    raise
                names.remove(name)
        for name in files:
            path = parent / name
            try:
                ensure_plain_path(path, root)
            except PermissionError:
                if not skip_unsafe:
                    raise
                continue
            yield path


def create_run_workspace(run_id: str | None = None) -> Path:
    run = run_id or uuid.uuid4().hex[:12]
    if not _RUN_ID_PATTERN.fullmatch(run):
        raise ValueError(f"Invalid sandbox run id: {run!r}")
    runs_root = canonical_path(SANDBOX_RUNS_DIR)
    workspace = canonical_path(runs_root / run)
    if not is_relative_to(workspace, runs_root):
        raise ValueError(f"Sandbox run workspace escaped runs directory: {workspace}")
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def copy_in_file(source: str | Path, workspace: str | Path, policy: SandboxPathPolicy) -> Path:
    source_path = ensure_allowed_path(source, policy.allowed_input_roots, purpose="input")
    if source_path.is_symlink():
        resolved = source_path.resolve(strict=True)
        ensure_allowed_path(resolved, policy.allowed_input_roots, purpose="input")
    if not source_path.is_file():
        raise FileNotFoundError(str(source_path))
    if source_path.stat().st_size > policy.max_copy_in_bytes:
        raise PermissionError("Sandbox copy-in size limit exceeded")
    target = canonical_path(workspace) / "input" / source_path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target)
    return target


def collect_copy_out(output_dir: str | Path, destination: str | Path, policy: SandboxPathPolicy) -> list[dict]:
    """Preflight bounded regular files and host conflicts before publishing changes."""
    source_root = Path(os.path.abspath(output_dir))
    destination_root = ensure_allowed_path(destination, policy.allowed_output_roots, purpose="output")
    pending: list[tuple[Path, Path, bytes, str | None]] = []
    copied: list[dict] = []
    total_bytes = 0
    for path in regular_workspace_files(source_root):
        relative = path.relative_to(source_root)
        baseline = policy.initial_files.get(str(relative).replace("\\", "/"))
        size = path.stat().st_size
        if size > policy.max_copy_out_bytes:
            raise PermissionError("Sandbox copy-out size limit exceeded")
        source = fingerprint_path(path, include_bytes=True, max_bytes=policy.max_copy_out_bytes)
        if not source.exists:
            raise PermissionError(f"Sandbox output changed during copy-out: {path}")
        if baseline == source.sha256:
            continue
        total_bytes += source.size
        if total_bytes > policy.max_copy_out_bytes:
            raise PermissionError("Sandbox copy-out size limit exceeded")
        target = destination_root / relative
        ensure_plain_path(target, destination_root)
        if is_blocked_path(target, policy.blocked_roots):
            raise PermissionError(f"Sandbox copy-out path is blocked: {target}")
        existing = fingerprint_path(target, max_bytes=policy.max_copy_out_bytes) if target.exists() else None
        current_hash = existing.sha256 if existing is not None else None
        if current_hash != baseline:
            raise PermissionError(f"Sandbox copy-out conflicts with a host change: {target}")
        pending.append((path, target, source.data or b"", current_hash))

    for path, target, data, expected_hash in pending:
        ensure_plain_path(target, destination_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        ensure_plain_path(target, destination_root)
        current = fingerprint_path(target, max_bytes=policy.max_copy_out_bytes)
        if current.sha256 != expected_hash:
            raise PermissionError(f"Sandbox copy-out conflicts with a host change: {target}")
        # Replace the directory entry; never truncate through a hard-linked leaf.
        stage = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".monaw-copy-", delete=False) as handle:
                stage = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            ensure_plain_path(target, destination_root)
            latest = fingerprint_path(target, max_bytes=policy.max_copy_out_bytes)
            if latest != current:
                raise PermissionError(f"Sandbox copy-out conflicts with a host change: {target}")
            os.chmod(stage, stat.S_IMODE(path.stat().st_mode) & 0o777)
            os.replace(stage, target)
        finally:
            if stage is not None:
                stage.unlink(missing_ok=True)
        copied.append({"source": str(path), "destination": str(target), "bytes": len(data)})
    return copied


def write_artifact_manifest(command_id: str, artifacts: dict) -> str:
    SANDBOX_MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SANDBOX_MANIFESTS_DIR / f"{command_id}.manifest.json"
    payload = {"command_id": command_id, "created_at": time.time(), "artifacts": artifacts}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return str(path)


def cleanup_old_runs(*, older_than_seconds: int) -> list[str]:
    if older_than_seconds < 0 or not SANDBOX_RUNS_DIR.exists():
        return []
    cutoff = time.time() - older_than_seconds
    removed: list[str] = []
    for child in SANDBOX_RUNS_DIR.iterdir():
        if not child.is_dir():
            continue
        try:
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child)
                removed.append(str(child))
        except FileNotFoundError:
            continue
    return removed
