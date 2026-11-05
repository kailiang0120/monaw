"""Resolve exact upload data at the controlled file-selection boundary."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
from typing import Any

from .sensitive_data import sensitive_path_labels
from app.agent.file_mutation.fingerprint import _created_ns


def _fingerprint(path: Path, maximum_bytes: int) -> dict[str, Any]:
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum_bytes:
            raise ValueError("upload_file_type_or_size_not_allowed")
        digest = hashlib.sha256()
        count = 0
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            count += len(chunk)
            if count > maximum_bytes:
                raise ValueError("upload_size_exceeded")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    final = path.stat()
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, _created_ns(s))
    # Compare ctime within the same fstat API; Windows Python 3.12 fstat and
    # path.stat use different ctime meanings, while birthtime agrees.
    if (identity(before) != identity(after) or before.st_ctime_ns != after.st_ctime_ns
            or identity(after) != identity(final)):
        raise ValueError("upload_file_changed")
    return {"path": str(path), "size_bytes": count, "sha256": digest.hexdigest(),
            "device": after.st_dev, "inode": after.st_ino,
            "mtime_ns": after.st_mtime_ns, "ctime_ns": after.st_ctime_ns}


def resolve_upload_scope(paths, workspace_roots=None, sensitive_paths=None) -> dict[str, Any]:
    """Pin canonical files, workspace membership, sensitivity and full content digest.

    Final revalidation narrows the race window; CDP opens paths independently and
    cannot provide an atomic handle-based upload guarantee.
    """
    from app.agent.conversation_workspace import current_workspace_dir, workspace_path
    from app.agent import controller_policy
    settings = controller_policy._load_runtime_settings()
    browser = settings.browser
    roots = workspace_roots if workspace_roots is not None else (getattr(browser, "upload_workspace_roots", None) or [current_workspace_dir()])
    sensitive = sensitive_paths if sensitive_paths is not None else getattr(browser, "sensitive_paths", [])
    maximum = int(getattr(browser, "max_upload_bytes", 512 * 1024 * 1024))
    roots = [Path(os.path.expandvars(str(root))).expanduser().resolve(strict=True) for root in roots]
    if not roots or not all(root.is_dir() for root in roots):
        raise ValueError("upload_workspace_required")
    if isinstance(paths, (str, bytes)) or not paths or len(paths) > 100:
        raise ValueError("upload_file_count_invalid")
    files = []
    seen = set()
    for raw in paths:
        path = workspace_path(str(raw)).resolve(strict=True)
        if not any(path.is_relative_to(root) for root in roots):
            raise ValueError("upload_outside_workspace")
        labels = sensitive_path_labels(path, sensitive)
        if labels:
            raise ValueError("upload_sensitive_file_blocked")
        if str(path).casefold() in seen:
            raise ValueError("upload_duplicate_file")
        seen.add(str(path).casefold())
        decision = controller_policy.resolve_permission(controller_policy.ActionType.READ, target_path=str(path))
        if not decision.allowed or decision.blocked or decision.requires_confirmation:
            raise ValueError("upload_file_read_not_permitted")
        files.append({**_fingerprint(path, maximum), "sensitivity": labels})
    total = sum(item["size_bytes"] for item in files)
    if total > maximum:
        raise ValueError("upload_size_exceeded")
    return {"files": files, "file_count": len(files), "size_bytes": total,
            "workspace_roots": [str(root) for root in roots], "classification": "ordinary_known_path"}


def revalidate_upload_scope(scope: dict[str, Any], *, workspace_roots=None) -> dict[str, Any]:
    # Re-read current settings and permissions as well as identity/content.
    current = resolve_upload_scope([item["path"] for item in scope.get("files", [])], workspace_roots=workspace_roots)
    if current != scope:
        raise ValueError("upload_scope_changed")
    return current
