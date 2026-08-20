"""Runtime data retention and deletion helpers."""

from __future__ import annotations

import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from app.agent import access_grant_broker, approval_broker
from app.agent.database import get_db
from app.agent.long_term_memory import get_long_term_memory, reset_long_term_memory
from app.agent.observability.recorder import (
    BACKEND_LOG_PATH,
    MAX_STORAGE_BYTES,
    RETENTION_DAYS,
    get_observability_recorder,
)
from app.agent.response_attachments import (
    attachment_registry_stats,
    clear_attachment_registry,
)
from app.agent.runtime_paths import MONAW_HOME_DIR, RUNTIME_DIR
from app.agent.ui_events import publish_ui_event

MAX_DIAGNOSTIC_BYTES = 200 * 1024 * 1024
MAX_BACKEND_LOG_BYTES = 5 * 1024 * 1024


def _is_within(path: Path, root: Path) -> bool:
    try:
        return path.resolve(strict=False).is_relative_to(root.resolve(strict=False))
    except (OSError, RuntimeError):
        return False


def _approved_root(path: Path) -> bool:
    return _is_within(path, RUNTIME_DIR) or _is_within(path, MONAW_HOME_DIR)


def _delete_file(path: Path) -> int:
    try:
        if path.is_symlink() or not path.is_file() or not _approved_root(path):
            return 0
        path.unlink()
        return 1
    except OSError:
        return 0


def _delete_tree_contents(root: Path) -> int:
    if root.is_symlink() or not _approved_root(root) or not root.exists():
        return 0
    deleted = 0
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        try:
            if path.is_symlink():
                continue
            if path.is_file():
                path.unlink()
                deleted += 1
            elif path.is_dir():
                path.rmdir()
        except OSError:
            continue
    return deleted


def _prune_files(
    roots: list[Path],
    *,
    max_age_days: int = RETENTION_DAYS,
    max_total_bytes: int = MAX_DIAGNOSTIC_BYTES,
    count_key: str = "diagnostic_files_deleted",
) -> dict[str, int]:
    cutoff = 0.0 if int(max_age_days) <= 0 else time.time() - int(max_age_days) * 24 * 60 * 60
    files: list[tuple[Path, float, int]] = []
    for root in roots:
        if root.is_symlink() or not _approved_root(root) or not root.exists():
            continue
        for path in root.rglob("*"):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
                files.append((path, stat.st_mtime, stat.st_size))
            except OSError:
                continue
    deleted = 0
    total = 0
    retained: list[tuple[Path, float, int]] = []
    for path, mtime, size in files:
        if mtime < cutoff:
            deleted += _delete_file(path)
        else:
            retained.append((path, mtime, size))
            total += size
    if total > max_total_bytes:
        for path, _mtime, size in sorted(retained, key=lambda item: item[1]):
            if total <= max_total_bytes:
                break
            deleted += _delete_file(path)
            total -= size
    return {count_key: deleted}


def _rotate_backend_log() -> dict[str, int]:
    deleted = 0
    truncated = 0
    cutoff = time.time() - RETENTION_DAYS * 24 * 60 * 60
    for path in RUNTIME_DIR.glob("backend.log*"):
        try:
            if path.is_symlink() or not path.is_file() or not _approved_root(path):
                continue
            stat = path.stat()
            if path != BACKEND_LOG_PATH and stat.st_mtime < cutoff:
                path.unlink()
                deleted += 1
                continue
            if stat.st_size > MAX_BACKEND_LOG_BYTES:
                if path == BACKEND_LOG_PATH:
                    path.write_text("", encoding="utf-8")
                    truncated += 1
                else:
                    path.unlink()
                    deleted += 1
        except OSError:
            continue
    return {
        "backend_logs_deleted": deleted,
        "backend_logs_truncated": truncated,
        "backend_log_max_bytes": MAX_BACKEND_LOG_BYTES,
    }


def _delete_memory_root() -> dict[str, int]:
    memory = get_long_term_memory()
    root = Path(memory.root)
    deleted: dict[str, int] = {}
    clear_records = getattr(memory, "clear_database_records", None)
    if callable(clear_records):
        deleted.update(clear_records())
    deleted["memory_files_deleted"] = _delete_tree_contents(root)
    try:
        if _approved_root(root):
            shutil.rmtree(root, ignore_errors=True)
    except OSError:
        pass
    reset_long_term_memory()
    return {"memory_files_deleted": deleted}


def _diagnostic_roots() -> list[Path]:
    return [
        MONAW_HOME_DIR / "browser" / "screenshots",
        MONAW_HOME_DIR / "browser" / "traces",
        MONAW_HOME_DIR / "browser" / "downloads",
        RUNTIME_DIR / "browser-use",
        RUNTIME_DIR / "harness_logs",
        RUNTIME_DIR / "mcp",
        RUNTIME_DIR / "mcp_bridge",
        RUNTIME_DIR / "policy",
    ]


def _artifact_roots() -> list[Path]:
    return [RUNTIME_DIR / "exec", RUNTIME_DIR / "sandbox"]


def _artifact_retention_days() -> int:
    try:
        from app.agent.settings_store import load_agent_settings
        from app.config import settings

        return max(1, int(load_agent_settings(settings).sandbox.preserve_artifacts_days))
    except Exception:
        return RETENTION_DAYS


def _delete_stale_database_backups() -> dict[str, int]:
    deleted = 0
    for path in RUNTIME_DIR.glob("agent.before-test-history-cleanup.*.db"):
        deleted += _delete_file(path)
    return {"stale_database_backups_deleted": deleted}


def enforce_runtime_retention() -> dict[str, int]:
    """Apply age/size retention across runtime diagnostics and operational stores."""
    artifact_days = _artifact_retention_days()
    counts: dict[str, int] = {
        "retention_days": RETENTION_DAYS,
        "artifact_retention_days": artifact_days,
        "observability_max_storage_bytes": MAX_STORAGE_BYTES,
        **attachment_registry_stats(),
    }
    get_observability_recorder().enforce_retention()
    counts.update(_rotate_backend_log())
    counts.update(_prune_files(_diagnostic_roots(), max_age_days=RETENTION_DAYS))
    counts.update(
        _prune_files(
            _artifact_roots(),
            max_age_days=artifact_days,
            count_key="artifact_files_deleted",
        )
    )
    counts.update(_delete_stale_database_backups())
    memory = get_long_term_memory()
    memory_retention = getattr(memory, "enforce_retention", None)
    if callable(memory_retention):
        counts.update(memory_retention())
    counts.update(approval_broker.enforce_retention(max_age_days=RETENTION_DAYS))
    counts.update(access_grant_broker.enforce_retention(max_age_days=RETENTION_DAYS))
    counts.update(get_db().enforce_scheduled_task_run_retention(max_age_days=RETENTION_DAYS))
    return counts


def delete_runtime_data() -> dict[str, object]:
    """Delete user-visible runtime data from observability, diagnostics, and support stores."""
    deleted: dict[str, int] = {}
    deleted.update(get_observability_recorder().delete_all())
    deleted.update(approval_broker.clear_all_tickets())
    deleted.update(access_grant_broker.clear_all_grants())
    deleted.update(clear_attachment_registry(delete_registered_files=True))
    deleted.update(get_db().clear_scheduled_task_outputs())
    deleted.update(_delete_memory_root())
    deleted.update(_delete_stale_database_backups())
    deleted.update({"backend_logs_deleted": 0})
    for path in RUNTIME_DIR.glob("backend.log*"):
        deleted["backend_logs_deleted"] += _delete_file(path)
    deleted.update(_prune_files(_diagnostic_roots(), max_age_days=0, max_total_bytes=0))
    deleted.update(
        _prune_files(
            _artifact_roots(),
            max_age_days=0,
            max_total_bytes=0,
            count_key="artifact_files_deleted",
        )
    )
    publish_ui_event("privacy.data_deleted", {"deleted": deleted})
    publish_ui_event("observability.changed", {"reason": "data_deleted"})
    publish_ui_event("memory.changed", {"reason": "data_deleted"})
    return {
        "ok": True,
        "deleted": deleted,
        "deleted_at": datetime.now(timezone.utc).isoformat(),
    }
