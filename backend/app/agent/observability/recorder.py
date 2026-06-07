"""Local-first structured observability for agent debugging."""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sqlite3
import threading
import time
import traceback as traceback_mod
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.agent.runtime_paths import RUNTIME_DIR
from app.agent.run_context import current_conversation_id


OBSERVABILITY_DIR = RUNTIME_DIR / "observability"
EVENTS_DIR = OBSERVABILITY_DIR / "events"
ERRORS_DIR = OBSERVABILITY_DIR / "errors"
EXPORTS_DIR = OBSERVABILITY_DIR / "exports"
DB_PATH = OBSERVABILITY_DIR / "observability.sqlite3"
PRICING_PATH = OBSERVABILITY_DIR / "model_pricing.json"
BACKEND_LOG_PATH = RUNTIME_DIR / "backend.log"

MAX_TEXT_CHARS = 200_000
MAX_TAIL_BYTES = 1_000_000
RETENTION_DAYS = 30
MAX_STORAGE_BYTES = 500 * 1024 * 1024

_CURRENT_RUN_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agent_current_observability_run_id",
    default="",
)
_RECORDER: "ObservabilityRecorder | None" = None
_HANDLER_INSTALLED = False

SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|authorization|cookie|session|bearer)",
    re.IGNORECASE,
)
BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE)
OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")
# High-confidence secret/PII value patterns kept in sync with
# long_term_memory._SENSITIVE_PATTERNS so secrets that the memory layer refuses to
# store are not leaked verbatim into observability dumps. (key, replacement) pairs.
_VALUE_REDACTIONS = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED SSN]"),
    (re.compile(r"\b(?:\d[ -]*?){13,16}\b"), "[REDACTED CARD]"),
)


@dataclass
class UsageStats:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    image_tokens: int = 0
    total_tokens: int = 0
    source: str = "unknown"

    def normalized(self) -> "UsageStats":
        total = self.total_tokens
        if total <= 0:
            total = (
                self.input_tokens
                + self.output_tokens
                + self.reasoning_tokens
                + self.image_tokens
            )
        return UsageStats(
            input_tokens=max(0, int(self.input_tokens or 0)),
            output_tokens=max(0, int(self.output_tokens or 0)),
            reasoning_tokens=max(0, int(self.reasoning_tokens or 0)),
            cached_tokens=max(0, int(self.cached_tokens or 0)),
            image_tokens=max(0, int(self.image_tokens or 0)),
            total_tokens=max(0, int(total or 0)),
            source=str(self.source or "unknown"),
        )

    def merge(self, other: "UsageStats | dict | None") -> "UsageStats":
        incoming = usage_from_any(other)
        sources = {self.source, incoming.source} - {"", "unknown"}
        source = "unknown"
        if len(sources) == 1:
            source = next(iter(sources))
        elif len(sources) > 1:
            source = "mixed"
        return UsageStats(
            input_tokens=self.input_tokens + incoming.input_tokens,
            output_tokens=self.output_tokens + incoming.output_tokens,
            reasoning_tokens=self.reasoning_tokens + incoming.reasoning_tokens,
            cached_tokens=self.cached_tokens + incoming.cached_tokens,
            image_tokens=self.image_tokens + incoming.image_tokens,
            total_tokens=self.total_tokens + incoming.total_tokens,
            source=source,
        ).normalized()

    def to_dict(self) -> dict:
        return asdict(self.normalized())


def usage_from_any(value: UsageStats | dict | None) -> UsageStats:
    if isinstance(value, UsageStats):
        return value.normalized()
    if not isinstance(value, dict):
        return UsageStats()
    return UsageStats(
        input_tokens=_to_int(value.get("input_tokens")),
        output_tokens=_to_int(value.get("output_tokens")),
        reasoning_tokens=_to_int(value.get("reasoning_tokens")),
        cached_tokens=_to_int(value.get("cached_tokens")),
        image_tokens=_to_int(value.get("image_tokens")),
        total_tokens=_to_int(value.get("total_tokens")),
        source=str(value.get("source") or "unknown"),
    ).normalized()


def current_run_id() -> str:
    return _CURRENT_RUN_ID.get()


def set_current_run_id(run_id: str):
    return _CURRENT_RUN_ID.set(run_id or "")


def reset_current_run_id(token) -> None:
    _CURRENT_RUN_ID.reset(token)


def infer_source(conversation_id: str) -> str:
    value = str(conversation_id or "").lower()
    if value.startswith("telegram"):
        return "telegram"
    if value.startswith("sched_") or value.startswith("scheduled"):
        return "scheduled"
    if value.startswith("replay_"):
        return "replay"
    return "desktop"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_preview(value: str, limit: int = MAX_TEXT_CHARS) -> str:
    text = BEARER_RE.sub("Bearer [REDACTED]", str(value or ""))
    text = OPENAI_KEY_RE.sub("sk-[REDACTED]", text)
    for pattern, replacement in _VALUE_REDACTIONS:
        text = pattern.sub(replacement, text)
    if len(text) > limit:
        return text[:limit] + "\n[truncated]"
    return text


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if SECRET_KEY_RE.search(key_text):
                redacted[key_text] = "[REDACTED]"
            else:
                redacted[key_text] = redact(item)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _safe_preview(value)
    return value


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(exclude_none=True)
    if hasattr(value, "__dict__"):
        return {
            key: item
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)


def _json_dumps(value: Any) -> str:
    return json.dumps(redact(value), ensure_ascii=False, default=_json_default)


def classify_failure(reason: str, status: str = "") -> str:
    text = f"{reason} {status}".lower()
    if "cancel" in text or "stopped by user" in text:
        return "cancelled"
    if "repeated_tool_call_blocked" in text or "repeated_blocked_tool_result" in text or "stalled_repeat_detected" in text:
        return "repeat_guard"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "iteration" in text or "budget" in text:
        return "iteration_limit"
    if "policy" in text or "blocked" in text or "denied" in text:
        return "policy_block"
    if "provider" in text or "llm" in text or "model" in text:
        return "provider_error"
    if "tool" in text:
        return "tool_error"
    if "telegram" in text or "mcp" in text or "browser" in text or "scheduler" in text:
        return "external_service"
    if "internal" in text or "exception" in text or "traceback" in text:
        return "internal_error"
    if str(status or "").lower() in {"complete", "success", "ok"}:
        return ""
    return "unknown"


class ObservabilityRecorder:
    """Append-only JSONL recorder with a SQLite dashboard index."""

    def __init__(self, root: Path = OBSERVABILITY_DIR) -> None:
        self.root = root
        self.events_dir = root / "events"
        self.errors_dir = root / "errors"
        self.exports_dir = root / "exports"
        self.db_path = root / "observability.sqlite3"
        self.pricing_path = root / "model_pricing.json"
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._ensure_dirs()
        self._init_db()
        self._ensure_pricing_file()
        self._cleanup_retention()

    def _ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.errors_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _connect(self):
        # Reuse a single long-lived connection rather than opening/closing (and
        # re-applying PRAGMAs) on every event. A turn fires dozens of events; the
        # per-event connection churn was pure overhead. The RLock (held by every
        # caller) serializes access, and busy_timeout hardens against contention.
        with self._lock:
            if self._connection is None:
                conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=5000")
                self._connection = conn
            conn = self._connection
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS observability_runs (
                    run_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    message_id TEXT DEFAULT '',
                    source TEXT DEFAULT 'desktop',
                    model TEXT DEFAULT '',
                    provider TEXT DEFAULT '',
                    status TEXT DEFAULT 'running',
                    failure_reason TEXT DEFAULT '',
                    failure_pattern TEXT DEFAULT '',
                    started_at TEXT NOT NULL,
                    finished_at TEXT DEFAULT '',
                    duration_ms INTEGER DEFAULT 0,
                    user_message TEXT DEFAULT '',
                    final_output TEXT DEFAULT '',
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    reasoning_tokens INTEGER DEFAULT 0,
                    cached_tokens INTEGER DEFAULT 0,
                    image_tokens INTEGER DEFAULT 0,
                    total_tokens INTEGER DEFAULT 0,
                    usage_source TEXT DEFAULT 'unknown',
                    estimated_cost_usd REAL DEFAULT 0,
                    cost_source TEXT DEFAULT 'unknown',
                    tool_count INTEGER DEFAULT 0,
                    tool_error_count INTEGER DEFAULT 0,
                    event_count INTEGER DEFAULT 0,
                    metadata_json TEXT DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS observability_events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT DEFAULT '',
                    conversation_id TEXT DEFAULT '',
                    message_id TEXT DEFAULT '',
                    event_type TEXT NOT NULL,
                    level TEXT DEFAULT 'info',
                    status TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    model TEXT DEFAULT '',
                    provider TEXT DEFAULT '',
                    tool_name TEXT DEFAULT '',
                    error_code TEXT DEFAULT '',
                    error_message TEXT DEFAULT '',
                    duration_ms INTEGER DEFAULT 0,
                    input_json TEXT DEFAULT '',
                    output_json TEXT DEFAULT '',
                    tokens_json TEXT DEFAULT '',
                    metadata_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observability_errors (
                    error_id TEXT PRIMARY KEY,
                    run_id TEXT DEFAULT '',
                    conversation_id TEXT DEFAULT '',
                    message_id TEXT DEFAULT '',
                    level TEXT DEFAULT 'error',
                    logger_name TEXT DEFAULT '',
                    module TEXT DEFAULT '',
                    error_type TEXT DEFAULT '',
                    message TEXT DEFAULT '',
                    traceback TEXT DEFAULT '',
                    metadata_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observability_replays (
                    replay_id TEXT PRIMARY KEY,
                    source_run_id TEXT NOT NULL,
                    replay_run_id TEXT DEFAULT '',
                    conversation_id TEXT DEFAULT '',
                    status_change TEXT DEFAULT '',
                    duration_delta_ms INTEGER DEFAULT 0,
                    token_delta INTEGER DEFAULT 0,
                    tool_sequence_diff TEXT DEFAULT '',
                    failure_reason_diff TEXT DEFAULT '',
                    metadata_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_obs_runs_started ON observability_runs(started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_obs_runs_status ON observability_runs(status);
                CREATE INDEX IF NOT EXISTS idx_obs_runs_conversation ON observability_runs(conversation_id);
                CREATE INDEX IF NOT EXISTS idx_obs_events_run ON observability_events(run_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_obs_errors_created ON observability_errors(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_obs_errors_run ON observability_errors(run_id);
                """
            )

    def _ensure_pricing_file(self) -> None:
        if self.pricing_path.exists():
            return
        payload = {
            "description": "Local editable pricing registry. Values are USD per 1M tokens.",
            "models": {},
        }
        self.pricing_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _cleanup_retention(self) -> None:
        try:
            cutoff_dt = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
            cutoff_iso = cutoff_dt.isoformat()
            cutoff_day = cutoff_dt.strftime("%Y-%m-%d")
            with self._lock, self._connect() as conn:
                conn.execute(
                    "DELETE FROM observability_events WHERE created_at < ?",
                    (cutoff_iso,),
                )
                conn.execute(
                    "DELETE FROM observability_errors WHERE created_at < ?",
                    (cutoff_iso,),
                )
                conn.execute(
                    "DELETE FROM observability_replays WHERE created_at < ?",
                    (cutoff_iso,),
                )
                conn.execute(
                    "DELETE FROM observability_runs WHERE started_at < ?",
                    (cutoff_iso,),
                )
            for directory in (self.events_dir, self.errors_dir):
                for path in directory.glob("*.jsonl"):
                    if path.stem < cutoff_day:
                        path.unlink(missing_ok=True)
            self._enforce_storage_cap()
        except Exception:
            pass

    def _enforce_storage_cap(self) -> None:
        # Stat each file once and reuse the result for the size sum, the mtime sort,
        # and the per-file size, instead of calling stat() up to three times per file.
        sized = [
            (path, path.stat())
            for path in self.root.rglob("*")
            if path.is_file() and path.name != self.db_path.name
        ]
        total = sum(st.st_size for _path, st in sized)
        if total <= MAX_STORAGE_BYTES:
            return
        for path, st in sorted(sized, key=lambda item: item[1].st_mtime):
            if total <= MAX_STORAGE_BYTES:
                break
            path.unlink(missing_ok=True)
            total -= st.st_size

    def start_run(
        self,
        *,
        conversation_id: str,
        user_message: str,
        source: str = "",
        model: str = "",
        provider: str = "",
        message_id: str = "",
        metadata: dict | None = None,
    ) -> str:
        run_id = f"run_{uuid.uuid4().hex}"
        created_at = now_iso()
        resolved_source = source or infer_source(conversation_id)
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO observability_runs (
                        run_id, conversation_id, message_id, source, model, provider,
                        status, started_at, user_message, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
                    """,
                    (
                        run_id,
                        conversation_id,
                        message_id,
                        resolved_source,
                        model,
                        provider,
                        created_at,
                        _safe_preview(user_message),
                        _json_dumps(metadata or {}),
                    ),
                )
            self.log_event(
                run_id=run_id,
                conversation_id=conversation_id,
                message_id=message_id,
                event_type="turn_started",
                source=resolved_source,
                model=model,
                provider=provider,
                input={"user_message": user_message},
                metadata=metadata or {},
            )
        except Exception:
            return run_id
        return run_id

    def log_event(
        self,
        *,
        event_type: str,
        run_id: str = "",
        conversation_id: str = "",
        message_id: str = "",
        level: str = "info",
        status: str = "",
        source: str = "",
        model: str = "",
        provider: str = "",
        tool_name: str = "",
        error_code: str = "",
        error_message: str = "",
        duration_ms: int | None = None,
        input: Any = None,
        output: Any = None,
        tokens: UsageStats | dict | None = None,
        metadata: dict | None = None,
    ) -> str:
        event_id = f"evt_{uuid.uuid4().hex}"
        created_at = now_iso()
        usage = usage_from_any(tokens)
        safe_error_code = _safe_preview(str(redact(error_code))) if error_code else ""
        safe_error_message = _safe_preview(str(redact(error_message))) if error_message else ""
        payload = {
            "event_id": event_id,
            "run_id": run_id,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "event_type": event_type,
            "level": level,
            "status": status,
            "source": source,
            "model": model,
            "provider": provider,
            "tool_name": tool_name,
            "error_code": safe_error_code,
            "error_message": safe_error_message,
            "duration_ms": int(duration_ms or 0),
            "input": input,
            "output": output,
            "tokens": usage.to_dict(),
            "metadata": metadata or {},
            "created_at": created_at,
        }
        try:
            self._append_jsonl(self.events_dir, created_at, payload)
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO observability_events (
                        event_id, run_id, conversation_id, message_id, event_type, level,
                        status, source, model, provider, tool_name, error_code, error_message,
                        duration_ms, input_json, output_json, tokens_json, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        run_id,
                        conversation_id,
                        message_id,
                        event_type,
                        level,
                        status,
                        source,
                        model,
                        provider,
                        tool_name,
                        safe_error_code,
                        safe_error_message,
                        int(duration_ms or 0),
                        _json_dumps(input) if input is not None else "",
                        _json_dumps(output) if output is not None else "",
                        _json_dumps(usage.to_dict()) if usage.total_tokens else "",
                        _json_dumps(metadata or {}),
                        created_at,
                    ),
                )
                if run_id:
                    conn.execute(
                        "UPDATE observability_runs SET event_count = event_count + 1 WHERE run_id = ?",
                        (run_id,),
                    )
        except Exception:
            pass
        return event_id

    def log_error(
        self,
        *,
        message: str,
        run_id: str = "",
        conversation_id: str = "",
        message_id: str = "",
        level: str = "error",
        logger_name: str = "",
        module: str = "",
        error_type: str = "",
        traceback: str = "",
        metadata: dict | None = None,
    ) -> str:
        error_id = f"err_{uuid.uuid4().hex}"
        created_at = now_iso()
        resolved_run_id = run_id or current_run_id()
        resolved_conversation_id = conversation_id or current_conversation_id()
        payload = {
            "error_id": error_id,
            "run_id": resolved_run_id,
            "conversation_id": resolved_conversation_id,
            "message_id": message_id,
            "level": level,
            "logger_name": logger_name,
            "module": module,
            "error_type": error_type,
            "message": message,
            "traceback": traceback,
            "metadata": metadata or {},
            "created_at": created_at,
        }
        try:
            self._append_jsonl(self.errors_dir, created_at, payload)
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO observability_errors (
                        error_id, run_id, conversation_id, message_id, level, logger_name,
                        module, error_type, message, traceback, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        error_id,
                        resolved_run_id,
                        resolved_conversation_id,
                        message_id,
                        level,
                        logger_name,
                        module,
                        error_type,
                        _safe_preview(message),
                        _safe_preview(traceback),
                        _json_dumps(metadata or {}),
                        created_at,
                    ),
                )
            if resolved_run_id:
                self.log_event(
                    run_id=resolved_run_id,
                    conversation_id=resolved_conversation_id,
                    message_id=message_id,
                    event_type="error",
                    level=level,
                    status="error",
                    error_code=error_type or "error",
                    error_message=message,
                    metadata=metadata or {},
                )
        except Exception:
            pass
        return error_id

    def finish_run(
        self,
        *,
        run_id: str,
        status: str,
        final_output: str = "",
        failure_reason: str = "",
        duration_ms: int = 0,
        usage: UsageStats | dict | None = None,
        tool_count: int | None = None,
        tool_error_count: int | None = None,
        message_id: str = "",
        metadata: dict | None = None,
    ) -> None:
        if not run_id:
            return
        finished_at = now_iso()
        normalized_usage = usage_from_any(usage)
        failure_pattern = classify_failure(failure_reason, status)
        try:
            with self._lock, self._connect() as conn:
                row = conn.execute(
                    "SELECT started_at, conversation_id, source, model, provider FROM observability_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if row and duration_ms <= 0:
                    duration_ms = _duration_ms(str(row["started_at"]), finished_at)
                if tool_count is None:
                    tool_count = int(
                        conn.execute(
                            """
                            SELECT COUNT(*) FROM observability_events
                            WHERE run_id = ? AND event_type = 'tool_call_finished'
                            """,
                            (run_id,),
                        ).fetchone()[0]
                    )
                if tool_error_count is None:
                    tool_error_count = int(
                        conn.execute(
                            """
                            SELECT COUNT(*) FROM observability_events
                            WHERE run_id = ? AND event_type = 'tool_call_finished'
                            AND status NOT IN ('', 'ok', 'complete', 'success')
                            """,
                            (run_id,),
                        ).fetchone()[0]
                    )
                estimated_cost_usd, cost_source = self._estimate_cost(
                    # Reuse model/provider already fetched into `row` above instead of
                    # making _estimate_cost re-SELECT the same run row.
                    provider=str(row["provider"] or "") if row else "",
                    model=str(row["model"] or "") if row else "",
                    usage=normalized_usage,
                    run_id=run_id,
                    conn=conn,
                )
                conn.execute(
                    """
                    UPDATE observability_runs
                    SET status = ?, failure_reason = ?, failure_pattern = ?, finished_at = ?,
                        duration_ms = ?, final_output = ?, input_tokens = ?, output_tokens = ?,
                        reasoning_tokens = ?, cached_tokens = ?, image_tokens = ?, total_tokens = ?,
                        usage_source = ?, estimated_cost_usd = ?, cost_source = ?,
                        tool_count = ?, tool_error_count = ?,
                        message_id = COALESCE(NULLIF(?, ''), message_id),
                        metadata_json = ?
                    WHERE run_id = ?
                    """,
                    (
                        status,
                        failure_reason,
                        failure_pattern,
                        finished_at,
                        int(duration_ms or 0),
                        _safe_preview(final_output),
                        normalized_usage.input_tokens,
                        normalized_usage.output_tokens,
                        normalized_usage.reasoning_tokens,
                        normalized_usage.cached_tokens,
                        normalized_usage.image_tokens,
                        normalized_usage.total_tokens,
                        normalized_usage.source,
                        estimated_cost_usd,
                        cost_source,
                        int(tool_count or 0),
                        int(tool_error_count or 0),
                        message_id,
                        _json_dumps(metadata or {}),
                        run_id,
                    ),
                )
            self.log_event(
                run_id=run_id,
                conversation_id=str(row["conversation_id"] or "") if row else "",
                event_type="turn_finished",
                status=status,
                source=str(row["source"] or "") if row else "",
                model=str(row["model"] or "") if row else "",
                provider=str(row["provider"] or "") if row else "",
                duration_ms=duration_ms,
                output={"assistant_output": final_output},
                tokens=normalized_usage,
                error_code=failure_reason if status != "complete" else "",
                metadata={"failure_pattern": failure_pattern, **(metadata or {})},
            )
        except Exception:
            pass

    def finish_open_run_for_conversation(
        self,
        *,
        conversation_id: str,
        status: str,
        failure_reason: str,
        final_output: str = "",
    ) -> None:
        try:
            row = self.latest_run_for_conversation(conversation_id, status="running")
            if not row:
                latest = self.latest_run_for_conversation(conversation_id)
                latest_status = str((latest or {}).get("status") or "").lower()
                latest_reason = str((latest or {}).get("failure_reason") or "").lower()
                if latest_status == "paused" and latest_reason == "cancelled":
                    row = latest
            if not row:
                return
            self.finish_run(
                run_id=str(row.get("run_id") or ""),
                status=status,
                failure_reason=failure_reason,
                final_output=final_output,
            )
        except Exception:
            pass

    def summary(self) -> dict:
        try:
            with self._lock, self._connect() as conn:
                totals = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS total_runs,
                        SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END) AS successful_runs,
                        SUM(CASE WHEN status != 'complete' THEN 1 ELSE 0 END) AS failed_runs,
                        SUM(total_tokens) AS total_tokens,
                        SUM(estimated_cost_usd) AS estimated_cost_usd,
                        AVG(CASE WHEN duration_ms > 0 THEN duration_ms END) AS average_duration_ms,
                        SUM(tool_error_count) AS tool_error_count
                    FROM observability_runs
                    """
                ).fetchone()
                by_day = self._fetch_all(
                    conn,
                    """
                    SELECT substr(started_at, 1, 10) AS day,
                           COUNT(*) AS runs,
                           SUM(total_tokens) AS tokens,
                           AVG(CASE WHEN duration_ms > 0 THEN duration_ms END) AS average_duration_ms
                    FROM observability_runs
                    GROUP BY substr(started_at, 1, 10)
                    ORDER BY day DESC
                    LIMIT 14
                    """,
                )
                error_reasons = self._fetch_all(
                    conn,
                    """
                    SELECT COALESCE(NULLIF(failure_pattern, ''), 'unknown') AS reason, COUNT(*) AS count
                    FROM observability_runs
                    WHERE status != 'complete'
                    GROUP BY COALESCE(NULLIF(failure_pattern, ''), 'unknown')
                    ORDER BY count DESC
                    LIMIT 8
                    """,
                )
                failing_tools = self._fetch_all(
                    conn,
                    """
                    SELECT tool_name, COUNT(*) AS count
                    FROM observability_events
                    WHERE event_type = 'tool_call_finished'
                      AND tool_name != ''
                      AND status NOT IN ('', 'ok', 'complete', 'success')
                    GROUP BY tool_name
                    ORDER BY count DESC
                    LIMIT 8
                    """,
                )
                model_usage = self._fetch_all(
                    conn,
                    """
                    SELECT COALESCE(NULLIF(model, ''), 'unknown') AS model,
                           COALESCE(NULLIF(provider, ''), 'unknown') AS provider,
                           COUNT(*) AS runs,
                           SUM(total_tokens) AS tokens
                    FROM observability_runs
                    GROUP BY COALESCE(NULLIF(model, ''), 'unknown'), COALESCE(NULLIF(provider, ''), 'unknown')
                    ORDER BY runs DESC
                    LIMIT 10
                    """,
                )
            total_runs = int(totals["total_runs"] or 0)
            successful_runs = int(totals["successful_runs"] or 0)
            success_rate = (successful_runs / total_runs) if total_runs else 0
            daily_trend = list(reversed(by_day))
            return {
                "total_runs": total_runs,
                "successful_runs": successful_runs,
                "success_rate": success_rate,
                "failed_runs": int(totals["failed_runs"] or 0),
                "total_tokens": int(totals["total_tokens"] or 0),
                "estimated_cost_usd": float(totals["estimated_cost_usd"] or 0),
                "average_duration_ms": int(totals["average_duration_ms"] or 0),
                "tool_error_count": int(totals["tool_error_count"] or 0),
                "token_usage_over_time": daily_trend,
                "duration_trend": daily_trend,
                "top_error_reasons": error_reasons,
                "top_failing_tools": failing_tools,
                "model_usage": model_usage,
                "storage_path": str(self.root),
            }
        except Exception:
            return {
                "total_runs": 0,
                "successful_runs": 0,
                "success_rate": 0,
                "failed_runs": 0,
                "total_tokens": 0,
                "estimated_cost_usd": 0,
                "average_duration_ms": 0,
                "tool_error_count": 0,
                "token_usage_over_time": [],
                "duration_trend": [],
                "top_error_reasons": [],
                "top_failing_tools": [],
                "model_usage": [],
                "storage_path": str(self.root),
            }

    def list_runs(
        self,
        *,
        status: str = "",
        source: str = "",
        model: str = "",
        q: str = "",
        limit: int = 100,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if source:
            clauses.append("source = ?")
            params.append(source)
        if model:
            clauses.append("model = ?")
            params.append(model)
        if q:
            like = f"%{q}%"
            clauses.append(
                "(run_id LIKE ? OR conversation_id LIKE ? OR user_message LIKE ? OR final_output LIKE ? OR failure_reason LIKE ?)"
            )
            params.extend([like, like, like, like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit or 100), 500)))
        try:
            with self._lock, self._connect() as conn:
                return self._fetch_all(
                    conn,
                    f"""
                    SELECT * FROM observability_runs
                    {where}
                    ORDER BY started_at DESC
                    LIMIT ?
                    """,
                    params,
                )
        except Exception:
            return []

    def get_run(self, run_id: str) -> dict | None:
        try:
            with self._lock, self._connect() as conn:
                run = conn.execute(
                    "SELECT * FROM observability_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if run is None:
                    return None
                events = self._fetch_all(
                    conn,
                    "SELECT * FROM observability_events WHERE run_id = ? ORDER BY created_at ASC",
                    (run_id,),
                )
                errors = self._fetch_all(
                    conn,
                    "SELECT * FROM observability_errors WHERE run_id = ? ORDER BY created_at ASC",
                    (run_id,),
                )
                replays = self._fetch_all(
                    conn,
                    "SELECT * FROM observability_replays WHERE source_run_id = ? ORDER BY created_at DESC",
                    (run_id,),
                )
            payload = _row_to_dict(run)
            payload["events"] = [self._decode_event(item) for item in events]
            payload["errors"] = [self._decode_metadata(item) for item in errors]
            payload["replays"] = [self._decode_metadata(item) for item in replays]
            payload["tool_sequence"] = [
                event.get("tool_name", "")
                for event in payload["events"]
                if event.get("event_type") == "tool_call_finished" and event.get("tool_name")
            ]
            return payload
        except Exception:
            return None

    def latest_run_for_conversation(self, conversation_id: str, status: str = "") -> dict | None:
        try:
            where = "conversation_id = ?"
            params: list[Any] = [conversation_id]
            if status:
                where += " AND status = ?"
                params.append(status)
            with self._lock, self._connect() as conn:
                row = conn.execute(
                    f"SELECT * FROM observability_runs WHERE {where} ORDER BY started_at DESC LIMIT 1",
                    params,
                ).fetchone()
            return _row_to_dict(row) if row else None
        except Exception:
            return None

    def list_errors(self, *, level: str = "", q: str = "", limit: int = 100) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if level:
            clauses.append("level = ?")
            params.append(level)
        if q:
            like = f"%{q}%"
            clauses.append("(message LIKE ? OR traceback LIKE ? OR logger_name LIKE ? OR module LIKE ?)")
            params.extend([like, like, like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit or 100), 500)))
        try:
            with self._lock, self._connect() as conn:
                rows = self._fetch_all(
                    conn,
                    f"""
                    SELECT * FROM observability_errors
                    {where}
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    params,
                )
            return [self._decode_metadata(item) for item in rows]
        except Exception:
            return []

    def read_backend_log_tail(self, tail: int = 400) -> dict:
        line_limit = max(1, min(int(tail or 400), 5000))
        if not BACKEND_LOG_PATH.exists():
            return {"path": str(BACKEND_LOG_PATH), "exists": False, "lines": [], "truncated": False}
        try:
            size = BACKEND_LOG_PATH.stat().st_size
            with BACKEND_LOG_PATH.open("rb") as handle:
                if size > MAX_TAIL_BYTES:
                    handle.seek(max(0, size - MAX_TAIL_BYTES))
                    chunk = handle.read()
                    truncated = True
                else:
                    chunk = handle.read()
                    truncated = False
            text = chunk.decode("utf-8", errors="replace")
            lines = text.splitlines()[-line_limit:]
            return {
                "path": str(BACKEND_LOG_PATH),
                "exists": True,
                "size_bytes": size,
                "lines": lines,
                "truncated": truncated,
            }
        except Exception as exc:
            return {
                "path": str(BACKEND_LOG_PATH),
                "exists": True,
                "lines": [],
                "truncated": False,
                "error": str(exc),
            }

    def record_replay(
        self,
        *,
        source_run_id: str,
        replay_run_id: str = "",
        conversation_id: str = "",
        status_change: str = "",
        duration_delta_ms: int = 0,
        token_delta: int = 0,
        tool_sequence_diff: str = "",
        failure_reason_diff: str = "",
        metadata: dict | None = None,
    ) -> dict:
        replay_id = f"replay_{uuid.uuid4().hex}"
        created_at = now_iso()
        payload = {
            "replay_id": replay_id,
            "source_run_id": source_run_id,
            "replay_run_id": replay_run_id,
            "conversation_id": conversation_id,
            "status_change": status_change,
            "duration_delta_ms": int(duration_delta_ms or 0),
            "token_delta": int(token_delta or 0),
            "tool_sequence_diff": tool_sequence_diff,
            "failure_reason_diff": failure_reason_diff,
            "metadata": metadata or {},
            "created_at": created_at,
        }
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO observability_replays (
                        replay_id, source_run_id, replay_run_id, conversation_id, status_change,
                        duration_delta_ms, token_delta, tool_sequence_diff, failure_reason_diff,
                        metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        replay_id,
                        source_run_id,
                        replay_run_id,
                        conversation_id,
                        status_change,
                        int(duration_delta_ms or 0),
                        int(token_delta or 0),
                        tool_sequence_diff,
                        failure_reason_diff,
                        _json_dumps(metadata or {}),
                        created_at,
                    ),
                )
        except Exception:
            pass
        return payload

    def export_debug_bundle(self, run_id: str) -> dict:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        payload = {
            "created_at": now_iso(),
            "run": run,
            "backend_log_tail": self.read_backend_log_tail(500),
        }
        path = self.exports_dir / f"{run_id}.json"
        path.write_text(json.dumps(redact(payload), ensure_ascii=False, indent=2), encoding="utf-8")
        return {"path": str(path), "run_id": run_id, "size_bytes": path.stat().st_size}

    def _estimate_cost(
        self,
        *,
        provider: str,
        model: str,
        usage: UsageStats,
        run_id: str = "",
        conn: sqlite3.Connection | None = None,
    ) -> tuple[float, str]:
        resolved_provider = provider
        resolved_model = model
        if run_id and conn is not None and (not provider or not model):
            row = conn.execute(
                "SELECT provider, model FROM observability_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row:
                resolved_provider = resolved_provider or str(row["provider"] or "")
                resolved_model = resolved_model or str(row["model"] or "")
        pricing = self._load_pricing()
        key = f"{resolved_provider}:{resolved_model}".lower()
        model_price = pricing.get(key) or pricing.get(resolved_model.lower())
        if not isinstance(model_price, dict):
            return 0.0, "unknown"
        input_rate = float(model_price.get("input_per_million") or 0)
        output_rate = float(model_price.get("output_per_million") or 0)
        reasoning_rate = float(model_price.get("reasoning_per_million") or output_rate)
        cost = (
            (usage.input_tokens / 1_000_000) * input_rate
            + (usage.output_tokens / 1_000_000) * output_rate
            + (usage.reasoning_tokens / 1_000_000) * reasoning_rate
        )
        return round(cost, 8), "estimated"

    def _load_pricing(self) -> dict:
        try:
            payload = json.loads(self.pricing_path.read_text(encoding="utf-8"))
            models = payload.get("models") if isinstance(payload, dict) else {}
            if isinstance(models, dict):
                return {str(key).lower(): value for key, value in models.items()}
        except Exception:
            pass
        return {}

    def _append_jsonl(self, directory: Path, created_at: str, payload: dict) -> None:
        day = created_at[:10] or datetime.now().strftime("%Y-%m-%d")
        path = directory / f"{day}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(_json_dumps(payload) + "\n")

    @staticmethod
    def _fetch_all(conn: sqlite3.Connection, sql: str, params: Any = ()) -> list[dict]:
        return [_row_to_dict(row) for row in conn.execute(sql, params).fetchall()]

    @staticmethod
    def _decode_metadata(item: dict) -> dict:
        decoded = dict(item)
        decoded["metadata"] = _json_loads(decoded.pop("metadata_json", "{}")) or {}
        return decoded

    @staticmethod
    def _decode_event(item: dict) -> dict:
        decoded = ObservabilityRecorder._decode_metadata(item)
        decoded["input"] = _json_loads(decoded.pop("input_json", ""))
        decoded["output"] = _json_loads(decoded.pop("output_json", ""))
        decoded["tokens"] = _json_loads(decoded.pop("tokens_json", ""))
        return decoded


class ObservabilityLoggingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            if getattr(record, "_monaw_observed", False):
                return
            exc_type = ""
            tb = ""
            if record.exc_info:
                exc_type = str(getattr(record.exc_info[0], "__name__", "") or "")
                tb = "".join(traceback_mod.format_exception(*record.exc_info))
            get_observability_recorder().log_error(
                message=record.getMessage(),
                level=record.levelname.lower(),
                logger_name=record.name,
                module=record.module,
                error_type=exc_type,
                traceback=tb,
                metadata={
                    "pathname": record.pathname,
                    "lineno": record.lineno,
                    "funcName": record.funcName,
                },
            )
        except Exception:
            pass


def install_logging_handler() -> None:
    global _HANDLER_INSTALLED
    if _HANDLER_INSTALLED:
        return
    handler = ObservabilityLoggingHandler(level=logging.ERROR)
    handler.setLevel(logging.ERROR)
    logging.getLogger().addHandler(handler)
    _HANDLER_INSTALLED = True


def get_observability_recorder() -> ObservabilityRecorder:
    global _RECORDER
    if _RECORDER is None:
        _RECORDER = ObservabilityRecorder()
    return _RECORDER


def _row_to_dict(row: sqlite3.Row | None) -> dict:
    if row is None:
        return {}
    return {key: row[key] for key in row.keys()}


def _json_loads(value: str) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def _duration_ms(started_at: str, finished_at: str) -> int:
    try:
        start = datetime.fromisoformat(started_at)
        finish = datetime.fromisoformat(finished_at)
        return max(0, round((finish - start).total_seconds() * 1000))
    except Exception:
        return 0
