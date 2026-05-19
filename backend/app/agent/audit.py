"""Audit logger - JSON-line event log with correlation IDs."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agent.runtime_paths import RUNTIME_DIR

_LOG_DIR = RUNTIME_DIR / "harness_logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)


class AuditLogger:
    """Append-only JSON-line logger for harness operations."""

    def __init__(self, log_dir: str | Path | None = None):
        self.log_dir = Path(log_dir) if log_dir else _LOG_DIR
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self.log_dir / "harness_audit.jsonl"

    def log(
        self,
        action: str,
        *,
        correlation_id: str = "",
        plan_id: str = "",
        data: dict[str, Any] | None = None,
        success: bool = True,
        error: str = "",
    ) -> str:
        """Write one audit event. Returns the correlation_id used."""
        cid = correlation_id or uuid.uuid4().hex[:12]
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "correlation_id": cid,
            "action": action,
            "plan_id": plan_id,
            "data": data or {},
            "success": success,
            "error": error,
        }
        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        return cid

    def read_events(
        self, correlation_id: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        """Read audit events, optionally filtered by correlation_id."""
        if not self._log_path.exists():
            return []
        events: list[dict[str, Any]] = []
        with open(self._log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if correlation_id and evt.get("correlation_id") != correlation_id:
                    continue
                events.append(evt)
                if len(events) >= limit:
                    break
        return events
