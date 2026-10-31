from __future__ import annotations

import os
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import yaml

from app.agent.memory_models import (
    LEGACY_LAYOUT,
    MemoryEntry,
    _FRONT_MATTER_RE,
    _ScopeFile,
    _dump_scope_file,
    _parse_scope_file,
    _workspace_key,
    logger,
    normalize_workspace,
)

class MarkdownRepository:
    # ------------------------------------------------------------------- storage

    def _retire_legacy_layout(self) -> None:
        """Move the pre-v2 memory layout aside once. Nothing is deleted."""
        legacy = [self.root / name for name in LEGACY_LAYOUT if (self.root / name).exists()]
        if not legacy:
            return
        target = self.root / "legacy-v1"
        target.mkdir(parents=True, exist_ok=True)
        for path in legacy:
            destination = target / path.name
            if destination.exists():
                destination = target / f"{path.name}-{secrets.token_hex(3)}"
            try:
                shutil.move(str(path), str(destination))
            except OSError as exc:
                logger.warning("Could not move legacy memory folder %s: %s", path, exc)
        logger.info("Moved the old memory layout to %s", target)

    @property
    def global_path(self) -> Path:
        return self.root / "global.md"

    def workspace_path(self, workspace: str) -> Path:
        return self.root / "workspaces" / f"{_workspace_key(normalize_workspace(workspace))}.md"

    def _load(self, scope: str, workspace: str = "") -> _ScopeFile:
        if scope == "global":
            path, workspace = self.global_path, ""
        else:
            workspace = normalize_workspace(workspace)
            path = self.workspace_path(workspace)
        with self._lock:
            try:
                stat = path.stat()
            except FileNotFoundError:
                return _ScopeFile(scope, path, workspace, [])
            signature = (stat.st_mtime_ns, stat.st_size)
            cached = self._cache.get(path)
            fallback = datetime.fromtimestamp(stat.st_mtime, timezone.utc).replace(microsecond=0).isoformat()
            try:
                entries = _parse_scope_file(path.read_text(encoding="utf-8"), scope=scope, workspace=workspace, fallback_time=fallback)
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning("Memory file %s could not be read: %s", path, exc)
                entries = []
            self._cache[path] = (signature, entries)
            return _ScopeFile(scope, path, workspace, list(entries))

    def _save(self, scope_file: _ScopeFile) -> None:
        with self._lock:
            scope_file.path.parent.mkdir(parents=True, exist_ok=True)
            temp = scope_file.path.with_name(f"{scope_file.path.name}.{secrets.token_hex(4)}.tmp")
            temp.write_text(_dump_scope_file(scope_file), encoding="utf-8")
            os.replace(temp, scope_file.path)
            self._generation += 1
            self._cache.pop(scope_file.path, None)

    def _scopes_for(self, workspace: str) -> list[_ScopeFile]:
        return [self._load("global"), self._load("workspace", workspace)]

    def entries(self, workspace: str = "", *, scope: str = "") -> list[MemoryEntry]:
        """Global memories plus the given workspace's, optionally limited to one scope."""
        return [
            entry
            for scope_file in self._scopes_for(workspace)
            if not scope or scope_file.scope == scope
            for entry in scope_file.entries
        ]

    def list_workspaces(self) -> list[dict[str, Any]]:
        workspaces: list[dict[str, Any]] = []
        for path in sorted((self.root / "workspaces").glob("*.md")):
            match = _FRONT_MATTER_RE.match(path.read_text(encoding="utf-8"))
            meta = yaml.safe_load(match.group(1)) if match else {}
            workspace = str((meta or {}).get("workspace") or "")
            if not workspace:
                continue
            workspaces.append({"workspace": workspace, "path": str(path), "count": len(self._load("workspace", workspace).entries)})
        return workspaces

    def find(self, memory_id: str, workspace: str = "") -> MemoryEntry | None:
        return next((entry for entry in self.entries(workspace) if entry.id == str(memory_id)), None)

