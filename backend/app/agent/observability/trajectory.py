"""JSONL trajectory logging for conversation analysis and training.

Records all conversations in ShareGPT-compatible format:
- Successful → trajectory_samples.jsonl
- Failed → failed_trajectories.jsonl

Each line is a complete conversation turn with metadata.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.agent.runtime_paths import RUNTIME_DIR

logger = logging.getLogger(__name__)

_RUNTIME_DIR = RUNTIME_DIR
_TRAJECTORY_DIR = _RUNTIME_DIR / "trajectories"
_TRAJECTORY_FILES = ("trajectory_samples.jsonl", "failed_trajectories.jsonl")
_SUCCESS_RETENTION = timedelta(days=1)
_FAILED_RETENTION = timedelta(days=3)


@dataclass
class TrajectoryEntry:
    """A single conversation turn for trajectory logging."""
    role: str  # "human" | "gpt" | "tool" | "system"
    content: str
    tool_name: str | None = None
    tool_input: dict | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class Trajectory:
    """A complete conversation trajectory."""
    conversation_id: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    model: str = ""
    provider: str = ""
    entries: list[TrajectoryEntry] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str = ""
    success: bool = True
    error: str = ""
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    metadata: dict = field(default_factory=dict)


class TrajectoryLogger:
    """Thread-safe JSONL trajectory logger."""

    def __init__(self, output_dir: Path | None = None) -> None:
        self._output_dir = output_dir or _TRAJECTORY_DIR
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._active: dict[str, Trajectory] = {}
        self.cleanup_expired()

    def start(
        self,
        conversation_id: str,
        model: str = "",
        provider: str = "",
        metadata: dict | None = None,
    ) -> None:
        """Begin recording a new trajectory."""
        with self._lock:
            self._active[conversation_id] = Trajectory(
                conversation_id=conversation_id,
                model=model,
                provider=provider,
                metadata=metadata or {},
            )

    def add_entry(
        self,
        conversation_id: str,
        role: str,
        content: str,
        tool_name: str | None = None,
        tool_input: dict | None = None,
    ) -> None:
        """Append an entry to an active trajectory."""
        with self._lock:
            traj = self._active.get(conversation_id)
            if traj is None:
                return
            traj.entries.append(TrajectoryEntry(
                role=role,
                content=content,
                tool_name=tool_name,
                tool_input=tool_input,
            ))

    def finish(
        self,
        conversation_id: str,
        success: bool = True,
        error: str = "",
        total_tokens: int = 0,
        total_cost_usd: float = 0.0,
    ) -> None:
        """Finalize and persist a trajectory."""
        with self._lock:
            traj = self._active.pop(conversation_id, None)
            if traj is None:
                return
            traj.finished_at = datetime.now(timezone.utc).isoformat()
            traj.success = success
            traj.error = error
            traj.total_tokens = total_tokens
            traj.total_cost_usd = total_cost_usd

        self._write(traj)

    def _write(self, traj: Trajectory) -> None:
        """Write trajectory to the appropriate JSONL file."""
        filename = "trajectory_samples.jsonl" if traj.success else "failed_trajectories.jsonl"
        filepath = self._output_dir / filename

        # ShareGPT-compatible format
        sharegpt = {
            "run_id": traj.run_id,
            "conversation_id": traj.conversation_id,
            "model": traj.model,
            "provider": traj.provider,
            "conversations": [
                {
                    "from": e.role,
                    "value": e.content,
                    **({"tool": e.tool_name} if e.tool_name else {}),
                    **({"tool_input": e.tool_input} if e.tool_input else {}),
                }
                for e in traj.entries
            ],
            "started_at": traj.started_at,
            "finished_at": traj.finished_at,
            "success": traj.success,
            "error": traj.error,
            "total_tokens": traj.total_tokens,
            "total_cost_usd": traj.total_cost_usd,
            "metadata": traj.metadata,
        }

        try:
            with self._lock:
                with open(filepath, "a", encoding="utf-8") as f:
                    f.write(json.dumps(sharegpt, ensure_ascii=False) + "\n")
            self.cleanup_expired()
        except Exception as exc:
            logger.warning("Failed to write trajectory: %s", exc)

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        self.cleanup_expired()
        rows = self._read_all()
        rows.sort(key=lambda item: str(item.get("finished_at") or item.get("started_at") or ""), reverse=True)
        return [self._summarize(row) for row in rows[: max(1, min(500, int(limit or 50)))]]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        self.cleanup_expired()
        wanted = str(run_id or "").strip()
        for row in self._read_all():
            if self._payload_run_id(row) == wanted:
                row.setdefault("run_id", wanted)
                return row
        return None

    def cleanup_expired(self, now: datetime | None = None) -> dict[str, int]:
        """Remove old trajectory rows based on success/failure retention."""
        cutoff_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        removed_by_file: dict[str, int] = {}
        with self._lock:
            for filename in _TRAJECTORY_FILES:
                path = self._output_dir / filename
                if not path.exists():
                    continue
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                    kept: list[str] = []
                    removed = 0
                    for line in lines:
                        if not line.strip():
                            removed += 1
                            continue
                        try:
                            payload = json.loads(line)
                        except json.JSONDecodeError:
                            kept.append(line)
                            continue
                        if isinstance(payload, dict) and self._is_expired(payload, cutoff_now):
                            removed += 1
                            continue
                        kept.append(line)
                    if removed:
                        path.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
                        removed_by_file[filename] = removed
                except Exception as exc:
                    logger.warning("Failed to clean up trajectories from %s: %s", path, exc)
        return removed_by_file

    def _read_all(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for filename in _TRAJECTORY_FILES:
            path = self._output_dir / filename
            if not path.exists():
                continue
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    payload = json.loads(line)
                    if isinstance(payload, dict):
                        payload["_source"] = filename
                        payload.setdefault("run_id", self._payload_run_id(payload))
                        rows.append(payload)
            except Exception as exc:
                logger.warning("Failed to read trajectories from %s: %s", path, exc)
        return rows

    @staticmethod
    def _payload_run_id(payload: dict[str, Any]) -> str:
        existing = str(payload.get("run_id") or "").strip()
        if existing:
            return existing
        seed = "|".join(
            [
                str(payload.get("conversation_id") or ""),
                str(payload.get("started_at") or ""),
                str(payload.get("finished_at") or ""),
            ]
        )
        return uuid.uuid5(uuid.NAMESPACE_URL, seed).hex

    @classmethod
    def _is_expired(cls, payload: dict[str, Any], now: datetime) -> bool:
        timestamp = cls._parse_timestamp(payload.get("finished_at")) or cls._parse_timestamp(payload.get("started_at"))
        if timestamp is None:
            return False
        retention = _SUCCESS_RETENTION if cls._payload_success(payload) else _FAILED_RETENTION
        return timestamp < now - retention

    @staticmethod
    def _payload_success(payload: dict[str, Any]) -> bool:
        value = payload.get("success", False)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() in {"1", "true", "yes", "ok", "success"}

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _summarize(cls, payload: dict[str, Any]) -> dict[str, Any]:
        entries = payload.get("conversations") if isinstance(payload.get("conversations"), list) else []
        first_user = next(
            (str(entry.get("value") or "") for entry in entries if entry.get("from") == "human"),
            "",
        )
        last_assistant = next(
            (
                str(entry.get("value") or "")
                for entry in reversed(entries)
                if entry.get("from") == "gpt"
            ),
            "",
        )
        tool_count = len([entry for entry in entries if entry.get("from") == "tool"])
        return {
            "run_id": cls._payload_run_id(payload),
            "conversation_id": str(payload.get("conversation_id") or ""),
            "model": str(payload.get("model") or ""),
            "provider": str(payload.get("provider") or ""),
            "started_at": str(payload.get("started_at") or ""),
            "finished_at": str(payload.get("finished_at") or ""),
            "success": bool(payload.get("success", False)),
            "error": str(payload.get("error") or ""),
            "entry_count": len(entries),
            "tool_count": tool_count,
            "total_tokens": int(payload.get("total_tokens") or 0),
            "total_cost_usd": float(payload.get("total_cost_usd") or 0.0),
            "last_user_message": first_user[:500],
            "final_assistant_message": last_assistant[:500],
            "source": str(payload.get("_source") or ""),
        }


_default_logger: TrajectoryLogger | None = None


def get_trajectory_logger() -> TrajectoryLogger:
    global _default_logger
    if _default_logger is None:
        _default_logger = TrajectoryLogger()
    return _default_logger
