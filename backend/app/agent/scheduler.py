from __future__ import annotations

import asyncio
import inspect
import hashlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.agent.database import get_db
from app.agent.harness.workers import unfinished_conversation_ids
from app.agent.ui_events import publish_ui_event

logger = logging.getLogger(__name__)

AgentRunner = Callable[..., AsyncIterator[dict[str, Any]]]
SettingsFactory = Callable[[], Any]
ScheduledRunOutcome = Literal["succeeded", "failed", "cancelled", "skipped"]


def _task_summary_status(outcome: ScheduledRunOutcome) -> str:
    return {"succeeded": "ok", "failed": "error", "cancelled": "cancelled", "skipped": "skipped"}[outcome]

_SCHEDULER_LEASE_SECONDS = 15 * 60
_SCHEDULER_LEASE_RENEW_SECONDS = _SCHEDULER_LEASE_SECONDS / 3
_DEFAULT_MAX_GLOBAL_CONCURRENCY = 4
_DEFAULT_MAX_PER_TASK_CONCURRENCY = 1
_DEFAULT_SHUTDOWN_GRACE_SECONDS = 2.0
_MAX_SCHEDULED_OUTPUT_CHARS = 20_000
_MAX_CONSECUTIVE_FAILURES = 5
_TRUNCATED_OUTPUT_SUFFIX = "\n\n[scheduled output truncated]"

_SERVICE: "ScheduledTaskService | None" = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _parse_datetime(value: str, *, tz_name: str = "UTC") -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_zoneinfo(tz_name))
    return parsed.astimezone(timezone.utc)


def _zoneinfo(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(name or "UTC"))
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def telegram_chat_allowed(chat_id: str) -> bool:
    """Only deliver scheduled output to Telegram users/chats the owner allowlisted."""
    try:
        target = int(str(chat_id or "").strip())
        from app.integrations.telegram.bridge import telegram_authorization_from_settings

        authorization = telegram_authorization_from_settings()
    except Exception:
        return False
    if authorization.allow_all:
        return True
    # A private chat's id equals the user's id, so allowed users count too.
    return target in authorization.allowed_chat_ids or target in authorization.allowed_user_ids


def set_scheduled_task_service(service: "ScheduledTaskService | None") -> None:
    global _SERVICE
    _SERVICE = service


def get_scheduled_task_service() -> "ScheduledTaskService | None":
    return _SERVICE


async def run_agent_stream(*args: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
    from app.agent.runtime import run_agent_stream as runtime_run_agent_stream

    kwargs.setdefault("execution_source", "scheduled")
    kwargs.setdefault("control_session_id", "scheduler")
    kwargs.setdefault("principal_id", "scheduler")
    kwargs.setdefault("permission_profile_id", "scheduler-restricted")
    kwargs.setdefault("interactive", False)
    async for event in runtime_run_agent_stream(*args, **kwargs):
        yield event


class ScheduledTaskService:
    def __init__(
        self,
        *,
        settings_factory: SettingsFactory,
        runner: AgentRunner = run_agent_stream,
        tick_interval: float = 30.0,
        max_global_concurrency: int = _DEFAULT_MAX_GLOBAL_CONCURRENCY,
        max_per_task_concurrency: int = _DEFAULT_MAX_PER_TASK_CONCURRENCY,
        shutdown_grace_seconds: float = _DEFAULT_SHUTDOWN_GRACE_SECONDS,
        max_queued_occurrences: int = 16,
    ) -> None:
        self.settings_factory = settings_factory
        self.runner = runner
        self._tick_interval = float(tick_interval)
        self._max_global_concurrency = max(1, int(max_global_concurrency))
        self._max_per_task_concurrency = max(1, int(max_per_task_concurrency))
        self._shutdown_grace_seconds = max(0.0, float(shutdown_grace_seconds))
        self._max_queued_occurrences = max(1, int(max_queued_occurrences))
        self._lease_owner = f"scheduler:{uuid.uuid4().hex}"
        self._task: asyncio.Task | None = None
        self._inflight: dict[str, list[asyncio.Task]] = {}
        self._run_tasks: dict[int, asyncio.Task] = {}
        self._entered_runs: set[int] = set()
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        get_db().recover_expired_scheduled_task_runs(_iso_utc(_utc_now()))
        self._task = asyncio.create_task(self._tick_loop(), name="scheduled-task-service")
        set_scheduled_task_service(self)
        logger.info("scheduled-tasks: service started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        inflight = [task for tasks in self._inflight.values() for task in tasks if not task.done()]
        pending = set(inflight)
        if pending and self._shutdown_grace_seconds > 0:
            _, pending = await asyncio.wait(pending, timeout=self._shutdown_grace_seconds)
        for task in pending:
            if not task.cancelling():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._inflight.clear()
        get_db().release_queued_scheduled_occurrences(self._lease_owner)
        if get_scheduled_task_service() is self:
            set_scheduled_task_service(None)
        logger.info("scheduled-tasks: service stopped")

    async def refresh(self, task_id: str = "") -> None:
        self._drain_pending()

    def status(self) -> dict[str, Any]:
        task = self._task
        return {
            "running": bool(task and not task.done() and not self._stop.is_set()),
            "stopping": self._stop.is_set(),
            "inflight_tasks": sum(1 for tasks in self._inflight.values() for item in tasks if not item.done()),
        }

    @staticmethod
    def operation_key(operation_key: str, requester: str) -> str:
        if not operation_key or len(operation_key) > 128:
            raise ValueError("Operation key must contain 1–128 characters")
        return "manual:" + hashlib.sha256(f"{requester}\0{operation_key}".encode()).hexdigest()

    def find_operation(self, task_id: str, operation_key: str, requester: str) -> dict[str, Any] | None:
        run = get_db().get_scheduled_operation(task_id, self.operation_key(operation_key, requester))
        return self._run_response(run) if run else None

    @staticmethod
    def _run_response(run: dict[str, Any]) -> dict[str, Any]:
        return {"run_id": run["id"], "conversation_id": run["conversation_id"]}

    async def run_now(self, task_id: str, *, operation_key: str = "", requester: str = "local") -> dict[str, Any]:
        key = self.operation_key(operation_key or uuid.uuid4().hex, requester)
        if self._stop.is_set():
            existing = get_db().get_scheduled_operation(task_id, key)
            if existing:
                return self._run_response(existing)
            raise RuntimeError("Scheduler is stopping")
        run = self._accept_occurrence(task_id, manual_key=key)
        self._cancel_requested_runs()
        self._drain_pending()
        return self._run_response(run)

    async def _tick_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._fire_due_tasks()
            except Exception as exc:
                logger.warning("scheduled-tasks: dispatch failed error_type=%s", type(exc).__name__)
            try:
                await asyncio.wait_for(self._stop.wait(), self._tick_interval)
            except asyncio.TimeoutError:
                pass

    async def _fire_due_tasks(self) -> None:
        now_iso = _iso_utc(_utc_now())
        get_db().recover_expired_scheduled_task_runs(now_iso)
        get_db().repair_scheduled_intent(now_iso, lambda row: self._next_iso(row, _utc_now()))
        self._cancel_requested_runs()
        self._drain_pending()
        for row in get_db().list_due_scheduled_tasks(now_iso):
            try:
                self._accept_occurrence(str(row["id"]), expected_due=str(row["next_run_at"]), revision=row["config_revision"])
                self._cancel_requested_runs()
                self._drain_pending()
            except KeyError:
                pass  # Deleted between the due-list read and acceptance.

    def _stopping_resources(self) -> tuple[set[str], int]:
        conversations = unfinished_conversation_ids()
        if not conversations:
            return set(), 0
        placeholders = ",".join("?" for _ in conversations)
        rows = get_db().fetchall(
            f"SELECT DISTINCT task_id, conversation_id FROM scheduled_task_runs WHERE conversation_id IN ({placeholders})",
            tuple(conversations),
        )
        return {row["task_id"] for row in rows}, len({row["conversation_id"] for row in rows})

    def _accept_occurrence(self, task_id: str, *, manual_key: str = "", expected_due: str = "", revision: int = 0):
        now = _utc_now()

        def build(row):
            due = _parse_datetime(expected_due) or now
            next_run = None
            occurrence = due
            if not manual_key:
                if row["schedule_kind"] == "interval":
                    seconds = int(row["interval_seconds"])
                    occurrence = due + timedelta(seconds=max(0, int((now - due).total_seconds() // seconds)) * seconds)
                    next_run = occurrence + timedelta(seconds=seconds)
                elif row["schedule_kind"] == "cron":
                    occurrence = max(due, self._cron_occurrence(row, now, forward=False))
                    next_run = self.compute_next_run(row, after=now)
            suffix = "shared" if row["reuse_conversation"] else uuid.uuid4().hex[:12]
            return {
                "conversation_id": f"sched_{task_id[:40]}_{suffix}",
                "conversation_title": f"Scheduled: {row['title']}",
                "scheduled_for": "" if manual_key else _iso_utc(occurrence),
                "coalesced": int(not manual_key and occurrence > due),
                "next_run_at": _iso_utc(next_run) if next_run else "",
            }

        blocked, stopping = self._stopping_resources()
        return get_db().accept_scheduled_occurrence(
            task_id, now_iso=_iso_utc(now), lease_owner=self._lease_owner, lease_expires_at=self._lease_expires_at(),
            build=build, manual_key=manual_key, expected_due=expected_due, expected_revision=revision,
            max_running=self._max_global_concurrency, blocked_tasks=blocked, stopping_count=stopping,
            max_queued=self._max_queued_occurrences,
        )

    def _cancel_requested_runs(self) -> None:
        for row in get_db().fetchall(
            "SELECT id FROM scheduled_task_runs WHERE status = 'running' AND cancel_requested = 1 AND lease_owner = ?",
            (self._lease_owner,),
        ):
            task = self._run_tasks.get(row["id"])
            if task and not task.done() and not task.cancelling():
                task.cancel()

    def _drain_pending(self) -> None:
        if self._stop.is_set():
            return
        now_iso = _iso_utc(_utc_now())
        get_db().cancel_disabled_scheduled_occurrences(now_iso)
        blocked, stopping = self._stopping_resources()
        for record in get_db().list_queued_scheduled_occurrences(self._lease_owner, now_iso):
            if record["task_id"] in blocked:
                continue
            if not get_db().start_scheduled_occurrence(
                record["id"], self._lease_owner, now_iso, self._lease_expires_at(), self._max_global_concurrency - stopping,
            ):
                continue
            row = json.loads(record["task_snapshot"])
            run = {"run_id": record["id"], "conversation_id": record["conversation_id"], "manual": bool(record["manual"])}
            task = asyncio.create_task(self._run_prepared_task(row, run), name=f"scheduled-task-run-{record['id']}")
            self._run_tasks[record["id"]] = task
            self._track_inflight(str(record["task_id"]), task)

            def finished(done, run_id=record["id"]):
                self._run_tasks.pop(run_id, None)
                if done.cancelled() and run_id not in self._entered_runs:
                    # Task.cancel() before its first loop turn never enters the
                    # coroutine's finally block. No tool has started; settle its
                    # owned reservation here so a replacement is not stranded.
                    saved = get_db().fetchone("SELECT * FROM scheduled_task_runs WHERE id = ?", (run_id,))
                    if saved:
                        get_db().finalize_scheduled_task_run(
                            task_id=saved["task_id"], run_id=run_id, conversation_id=saved["conversation_id"],
                            lease_owner=self._lease_owner, finished_at=_iso_utc(_utc_now()), status="cancelled",
                            final_text="", error="cancelled before execution",
                            prepare_task_update=lambda current: {"last_run_status": "cancelled"},
                        )
                self._entered_runs.discard(run_id)
                if not done.cancelled() and done.exception() is not None:
                    logger.warning("scheduled-tasks: run wrapper failed run=%s error_type=%s", run_id, type(done.exception()).__name__)
                try:
                    self._drain_pending()
                except Exception as exc:
                    logger.warning("scheduled-tasks: queue dispatch failed error_type=%s", type(exc).__name__)

            task.add_done_callback(finished)

    def _lease_expires_at(self) -> str:
        return _iso_utc(_utc_now() + timedelta(seconds=_SCHEDULER_LEASE_SECONDS))

    async def _keep_run_lease(self, run_id: int, execution: asyncio.Task | None, lost: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(_SCHEDULER_LEASE_RENEW_SECONDS)
            try:
                renewed = get_db().renew_scheduled_task_run_lease(
                    run_id, lease_owner=self._lease_owner, now_iso=_iso_utc(_utc_now()),
                    lease_expires_at=self._lease_expires_at(),
                )
            except Exception as exc:
                logger.warning("scheduled-tasks: lease renewal failed run=%s error_type=%s", run_id, type(exc).__name__)
                renewed = False
            if not renewed:
                lost.set()
                if execution is not None:
                    execution.cancel()
                return

    def _track_inflight(self, task_id: str, task: asyncio.Task) -> None:
        self._inflight.setdefault(task_id, []).append(task)

        def clear(done: asyncio.Task, task_id: str = task_id) -> None:
            tasks = self._inflight.get(task_id)
            if tasks is None:
                return
            self._inflight[task_id] = [task for task in tasks if task is not done]
            if not self._inflight[task_id]:
                self._inflight.pop(task_id, None)

        task.add_done_callback(clear)

    async def _run_prepared_task(self, row: dict[str, Any], run: dict[str, Any]) -> None:
        self._entered_runs.add(int(run["run_id"]))
        task_id = str(row["id"])
        run_id = int(run["run_id"])
        conversation_id = str(run["conversation_id"])
        final_text_parts: list[str] = []
        final_text_chars = 0
        final_text_truncated = False
        done_summary = ""
        error_text = ""
        status: ScheduledRunOutcome = "succeeded"
        heartbeat: asyncio.Task | None = None
        lease_lost = asyncio.Event()

        try:
            if not get_db().renew_scheduled_task_run_lease(
                run_id, lease_owner=self._lease_owner, now_iso=_iso_utc(_utc_now()),
                lease_expires_at=self._lease_expires_at(),
                start=True, manual=bool(run["manual"]),
            ):
                status = "cancelled"
                error_text = "task or lease unavailable before execution"
                return
            heartbeat = asyncio.create_task(
                self._keep_run_lease(run_id, asyncio.current_task(), lease_lost),
                name=f"scheduled-task-lease-{run_id}",
            )
            settings = self.settings_factory()
            async for event in self.runner(
                message=str(row.get("prompt") or ""),
                conversation_id=conversation_id,
                settings=settings,
                attachments=None,
                execution_source="scheduled",
                control_session_id="scheduler",
                principal_id=str(row.get("owner_principal_id") or f"scheduled-task:{task_id}"),
                permission_profile_id=(
                    str(row.get("permission_profile_id") or "")
                    if str(row.get("owner_principal_id") or "")
                    else f"scheduled-task:{task_id}:restricted"
                ),
                interactive=False,
            ):
                event_name = str(event.get("event") or "")
                data = event.get("data") if isinstance(event.get("data"), dict) else {}
                if event_name == "token":
                    token = str(data.get("content") or "")
                    if token and final_text_chars < _MAX_SCHEDULED_OUTPUT_CHARS:
                        remaining = _MAX_SCHEDULED_OUTPUT_CHARS - final_text_chars
                        final_text_parts.append(token[:remaining])
                        final_text_chars += min(len(token), remaining)
                        final_text_truncated = final_text_truncated or len(token) > remaining
                    elif token:
                        final_text_truncated = True
                elif event_name == "done":
                    done_summary = str(data.get("summary") or "").strip()
                    if data.get("status") == "error" or data.get("incomplete"):
                        status = "failed"
                        error_text = str(data.get("reason_code") or "scheduled task ended incomplete")
                elif event_name == "error":
                    status = "failed"
                    error_text = str(data.get("message") or data.get("code") or "scheduled task error")
        except asyncio.CancelledError:
            status = "cancelled"
            error_text = "scheduler lease lost" if lease_lost.is_set() else "cancelled"
            raise
        except Exception as exc:
            logger.exception("scheduled-tasks: task %s failed", task_id)
            status = "failed"
            error_text = str(exc)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            final_text = done_summary or "".join(final_text_parts).strip()
            if final_text_truncated and not final_text.endswith(_TRUNCATED_OUTPUT_SUFFIX):
                final_text = f"{final_text[:_MAX_SCHEDULED_OUTPUT_CHARS].rstrip()}{_TRUNCATED_OUTPUT_SUFFIX}"
            final_text = final_text[: _MAX_SCHEDULED_OUTPUT_CHARS + len(_TRUNCATED_OUTPUT_SUFFIX)]
            await self._finish_run(
                row,
                run_id=run_id,
                conversation_id=conversation_id,
                status=status,
                final_text=final_text,
                error=error_text,
            )

    async def _finish_run(
        self,
        row: dict[str, Any],
        *,
        run_id: int,
        conversation_id: str,
        status: ScheduledRunOutcome,
        final_text: str,
        error: str,
    ) -> None:
        now = _utc_now()
        now_iso = _iso_utc(now)
        def prepare_task_update(current: dict[str, Any]) -> dict[str, Any]:
            next_run = self.compute_next_run(current, after=now) if current["enabled"] else None
            failures = int(current.get("consecutive_failures") or 0)
            if status == "succeeded":
                failures = 0
            elif status == "failed":
                failures += 1
            enabled = int(current["enabled"])
            last_status = _task_summary_status(status)
            if failures >= _MAX_CONSECUTIVE_FAILURES:
                enabled = 0
                next_run = None
                last_status = "dead_letter"
            elif next_run is None and current["schedule_kind"] == "once":
                enabled = 0
            return {
                "last_run_at": now_iso, "last_run_status": last_status,
                "last_run_conversation_id": conversation_id,
                "next_run_at": _iso_utc(next_run) if next_run else "",
                "consecutive_failures": failures, "enabled": enabled,
            }

        current = get_db().finalize_scheduled_task_run(
            task_id=str(row["id"]), run_id=run_id, conversation_id=conversation_id,
            lease_owner=self._lease_owner,
            finished_at=now_iso,
            status=status,
            final_text=final_text,
            error=error,
            prepare_task_update=prepare_task_update,
        )
        if current is None:
            return
        publish_ui_event(
            "scheduled_tasks.changed",
            {"task_id": str(row["id"]), "action": "run_finished", "status": current["last_run_status"]},
        )
        publish_ui_event(
            "conversation.changed",
            {"conversation_id": conversation_id, "action": "scheduled_run_finished"},
        )
        publish_ui_event("usage.changed", {"conversation_id": conversation_id})
        publish_ui_event("observability.changed", {"conversation_id": conversation_id})

        if (
            status not in {"skipped", "cancelled"}
            and int(row.get("notify_telegram") or 0)
            and str(row.get("telegram_chat_id") or "").strip()
        ):
            try:
                await self._notify_telegram(row, final_text=final_text, status=status, error=error)
            except Exception as exc:
                # Delivery is separate from execution. Transport exceptions can
                # contain URLs/tokens, so log only the type and correlation IDs.
                logger.warning(
                    "scheduled-tasks: notification delivery failed task=%s run=%s error_type=%s",
                    row["id"], run_id, type(exc).__name__,
                )

    async def _notify_telegram(
        self,
        row: dict[str, Any],
        *,
        final_text: str,
        status: ScheduledRunOutcome,
        error: str,
    ) -> None:
        try:
            from app.integrations.telegram.agent_bridge import simplify_telegram_text, split_telegram_message
            from app.integrations.telegram.service import get_telegram_bot
        except Exception:
            logger.debug("scheduled-tasks: Telegram modules unavailable", exc_info=True)
            return

        bot = get_telegram_bot()
        if bot is None:
            return
        if not telegram_chat_allowed(str(row.get("telegram_chat_id") or "")):
            logger.warning(
                "scheduled-tasks: refusing Telegram notification for task %s to a chat outside the allowlist",
                row.get("id"),
            )
            return
        title = str(row.get("title") or "Scheduled task").strip()
        body = (
            final_text.strip() or "Scheduled task completed successfully (no output)."
            if status == "succeeded"
            else f"Scheduled task failed: {error or 'unknown error'}"
        )
        text = simplify_telegram_text(f"{title}\n\n{body}".strip())
        chat_id = str(row.get("telegram_chat_id") or "").strip()
        for chunk in split_telegram_message(text):
            result = bot.send_message(chat_id=chat_id, text=chunk)
            if inspect.isawaitable(result):
                await result

    @staticmethod
    def _cron_occurrence(task_row: dict[str, Any], reference: datetime, *, forward: bool) -> datetime:
        from croniter import croniter

        tz = _zoneinfo(task_row.get("timezone") or "UTC")
        local = reference.astimezone(tz)
        base = local.replace(tzinfo=None)
        if not forward:
            if local.fold:
                # During a repeated hour the first fold's later wall times
                # have already occurred in UTC. Consider them when coalescing.
                base += local.replace(fold=0).utcoffset() - local.replace(fold=1).utcoffset()
            base += timedelta(microseconds=1)
        iterator = croniter(task_row["cron_expr"], base)
        for _ in range(10000):
            naive = iterator.get_next(datetime) if forward else iterator.get_prev(datetime)
            candidate = naive.replace(tzinfo=tz, fold=0).astimezone(timezone.utc)
            # Round-trip rejects nonexistent local times. fold=0 chooses only
            # the first occurrence of an ambiguous local time.
            if candidate.astimezone(tz).replace(tzinfo=None) != naive:
                continue
            if (forward and candidate > reference) or (not forward and candidate <= reference):
                return candidate
        raise ValueError("Cron did not resolve to a valid local occurrence")

    @classmethod
    def _next_iso(cls, row: dict[str, Any], after: datetime) -> str:
        value = cls.compute_next_run(row, after=after)
        return _iso_utc(value) if value else ""

    @staticmethod
    def compute_next_run(task_row: dict[str, Any], *, after: datetime) -> datetime | None:
        if after.tzinfo is None:
            after = after.replace(tzinfo=timezone.utc)
        after_utc = after.astimezone(timezone.utc)
        kind = str(task_row.get("schedule_kind") or "").strip()
        tz_name = str(task_row.get("timezone") or "UTC").strip() or "UTC"

        if kind == "cron":
            expr = str(task_row.get("cron_expr") or "").strip()
            if not expr:
                return None
            return ScheduledTaskService._cron_occurrence(task_row, after_utc, forward=True)

        if kind == "interval":
            seconds = int(task_row.get("interval_seconds") or 0)
            if seconds <= 0:
                return None
            base = _parse_datetime(str(task_row.get("last_run_at") or ""), tz_name=tz_name) or after_utc
            steps = max(1, int((after_utc - base).total_seconds() // seconds) + 1)
            next_run = base + timedelta(seconds=steps * seconds)
            return next_run.astimezone(timezone.utc)

        if kind == "once":
            run_at = _parse_datetime(str(task_row.get("run_at") or ""), tz_name=tz_name)
            if run_at is None or run_at <= after_utc:
                return None
            return run_at.astimezone(timezone.utc)

        return None
