from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from app.agent.runtime_paths import RUNTIME_DIR

SANDBOX_RUNTIME_DIR = RUNTIME_DIR / "sandbox"
SANDBOX_RUNS_DIR = SANDBOX_RUNTIME_DIR / "runs"
SANDBOX_MANIFESTS_DIR = SANDBOX_RUNTIME_DIR / "manifests"
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
DEFAULT_MAX_COPY_OUT_BYTES = 1048576


@dataclass(slots=True)
class SandboxPathPolicy:
    allowed_input_roots: list[Path] = field(default_factory=list)
    allowed_output_roots: list[Path] = field(default_factory=list)
    max_copy_out_bytes: int = DEFAULT_MAX_COPY_OUT_BYTES
    max_copy_in_bytes: int = 104857600
    initial_files: dict[str, tuple[int, int]] = field(default_factory=dict)

    @classmethod
    def from_strings(
        cls,
        *,
        allowed_input_roots: Iterable[str] = (),
        allowed_output_roots: Iterable[str] = (),
        max_copy_out_bytes: int = DEFAULT_MAX_COPY_OUT_BYTES,
        max_copy_in_bytes: int = 104857600,
        initial_files: dict[str, tuple[int, int]] | None = None,
    ) -> "SandboxPathPolicy":
        return cls(
            allowed_input_roots=[canonical_path(root) for root in allowed_input_roots],
            allowed_output_roots=[canonical_path(root) for root in allowed_output_roots],
            max_copy_out_bytes=max_copy_out_bytes,
            max_copy_in_bytes=max_copy_in_bytes,
            initial_files=dict(initial_files or {}),
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


def create_run_workspace(run_id: str | None = None) -> Path:
    run = run_id or uuid.uuid4().hex[:12]
    if not _RUN_ID_PATTERN.fullmatch(run):
        raise ValueError(f"Invalid sandbox run id: {run!r}")
    runs_root = canonical_path(SANDBOX_RUNS_DIR)
    workspace = canonical_path(runs_root / run)
    if not is_relative_to(workspace, runs_root):
        raise ValueError(f"Sandbox run workspace escaped runs directory: {workspace}")
    (workspace / "input").mkdir(parents=True, exist_ok=True)
    (workspace / "output").mkdir(parents=True, exist_ok=True)
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
    source_root = canonical_path(output_dir)
    destination_root = ensure_allowed_path(destination, policy.allowed_output_roots, purpose="output")
    pending: list[tuple[Path, Path, int]] = []
    copied: list[dict] = []
    total_bytes = 0
    for path in source_root.rglob("*"):
        if path.is_symlink():
            raise PermissionError(f"Symlink copy-out is not allowed: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(source_root)
        baseline = policy.initial_files.get(str(relative).replace("\\", "/"))
        stat = path.stat()
        if baseline == (stat.st_size, stat.st_mtime_ns):
            continue
        size = stat.st_size
        total_bytes += size
        if total_bytes > policy.max_copy_out_bytes:
            raise PermissionError("Sandbox copy-out size limit exceeded")
        target = destination_root / relative
        pending.append((path, target, size))

    for path, target, size in pending:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append({"source": str(path), "destination": str(target), "bytes": size})
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
