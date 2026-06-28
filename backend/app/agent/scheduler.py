from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.agent.database import get_db
from app.agent.ui_events import publish_ui_event

logger = logging.getLogger(__name__)

AgentRunner = Callable[..., AsyncIterator[dict[str, Any]]]
SettingsFactory = Callable[[], Any]

_SCHEDULER_LEASE_SECONDS = 15 * 60
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
    ) -> None:
        self.settings_factory = settings_factory
        self.runner = runner
        self._tick_interval = float(tick_interval)
        self._max_global_concurrency = max(1, int(max_global_concurrency))
        self._max_per_task_concurrency = max(1, int(max_per_task_concurrency))
        self._shutdown_grace_seconds = max(0.0, float(shutdown_grace_seconds))
        self._lease_owner = f"scheduler:{uuid.uuid4().hex}"
        self._task: asyncio.Task | None = None
        self._inflight: dict[str, list[asyncio.Task]] = {}
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
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._inflight.clear()
        if get_scheduled_task_service() is self:
            set_scheduled_task_service(None)
        logger.info("scheduled-tasks: service stopped")

    async def refresh(self, task_id: str = "") -> None:
        # The service reads due work from SQLite every tick. This hook keeps the
        # API contract explicit while avoiding an in-memory schedule cache.
        _ = task_id

    def status(self) -> dict[str, Any]:
        task = self._task
        return {
            "running": bool(task and not task.done() and not self._stop.is_set()),
            "stopping": self._stop.is_set(),
            "inflight_tasks": sum(1 for tasks in self._inflight.values() for item in tasks if not item.done()),
        }

    async def run_now(self, task_id: str) -> dict[str, Any]:
        row = get_db().get_scheduled_task(task_id)
        if row is None:
            raise KeyError(task_id)
        for previous in self._tasks_for(task_id):
            if not previous.done():
                previous.cancel()
        run = self._prepare_run(row, manual=True)
        task = asyncio.create_task(
            self._run_prepared_task(row, run),
            name=f"scheduled-task-run-{task_id}",
        )
        self._track_inflight(task_id, task)
        return {"run_id": run["run_id"], "conversation_id": run["conversation_id"]}

    async def _tick_loop(self) -> None:
        while not self._stop.is_set():
            await self._fire_due_tasks()
            try:
                await asyncio.wait_for(self._stop.wait(), self._tick_interval)
            except asyncio.TimeoutError:
                pass

    async def _fire_due_tasks(self) -> None:
        now_iso = _iso_utc(_utc_now())
        for row in get_db().list_due_scheduled_tasks(now_iso):
            task_id = str(row["id"])
            if not get_db().claim_due_scheduled_task(
                task_id,
                expected_next_run_at=str(row.get("next_run_at") or ""),
                now_iso=now_iso,
            ):
                continue
            if self._global_inflight_count() >= self._max_global_concurrency:
                await self._skip_throttled_run(row, reason="global_concurrency_limit")
                continue
            if len(self._tasks_for(task_id)) >= self._max_per_task_concurrency:
                await self._skip_throttled_run(row, reason="per_task_concurrency_limit")
                continue
            previous = self._latest_inflight(task_id)

            policy = str(row.get("overlap_policy") or "skip")
            if previous is not None:
                if policy == "skip":
                    await self._skip_overlapping_run(row)
                    continue
                if policy == "cancel_previous":
                    for running in self._tasks_for(task_id):
                        if not running.done():
                            running.cancel()
                elif policy == "queue":
                    run = self._prepare_run(row, manual=False, initial_status="queued")
                    task = asyncio.create_task(
                        self._run_after_previous(task_id, previous, row, run),
                        name=f"scheduled-task-queued-{task_id}",
                    )
                    self._track_inflight(task_id, task)
                    continue

            run = self._prepare_run(row, manual=False)
            task = asyncio.create_task(
                self._run_prepared_task(row, run),
                name=f"scheduled-task-run-{task_id}",
            )
            self._track_inflight(task_id, task)

    def _global_inflight_count(self) -> int:
        return sum(1 for tasks in self._inflight.values() for item in tasks if not item.done())

    def _lease_expires_at(self) -> str:
        return _iso_utc(_utc_now() + timedelta(seconds=_SCHEDULER_LEASE_SECONDS))

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

    def _tasks_for(self, task_id: str) -> list[asyncio.Task]:
        tasks = [task for task in self._inflight.get(task_id, []) if not task.done()]
        if tasks:
            self._inflight[task_id] = tasks
        else:
            self._inflight.pop(task_id, None)
        return tasks

    def _latest_inflight(self, task_id: str) -> asyncio.Task | None:
        tasks = self._tasks_for(task_id)
        return tasks[-1] if tasks else None

    async def _run_after_previous(
        self,
        task_id: str,
        previous: asyncio.Task,
        row: dict[str, Any],
        run: dict[str, Any],
    ) -> None:
        try:
            await asyncio.shield(previous)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                await self._finish_run(
                    row,
                    run_id=int(run["run_id"]),
                    conversation_id=str(run["conversation_id"]),
                    status="cancelled",
                    final_text="",
                    error="cancelled",
                )
                raise
            pass
        except Exception:
            logger.debug("scheduled-tasks: queued run ignored previous failure", exc_info=True)
        if self._stop.is_set():
            await self._finish_run(
                row,
                run_id=int(run["run_id"]),
                conversation_id=str(run["conversation_id"]),
                status="cancelled",
                final_text="",
                error="scheduler stopped before queued run started",
            )
            return
        latest = get_db().get_scheduled_task(task_id) or row
        await self._run_prepared_task(latest, run)

    async def _skip_throttled_run(self, row: dict[str, Any], *, reason: str) -> None:
        await self._mark_task_skipped(row, reason=reason)

    async def _skip_overlapping_run(self, row: dict[str, Any]) -> None:
        await self._mark_task_skipped(row, reason="skipped")

    async def _mark_task_skipped(self, row: dict[str, Any], *, reason: str) -> None:
        now = _utc_now()
        next_run = self.compute_next_run(row, after=now)
        fields: dict[str, Any] = {
            "last_run_at": _iso_utc(now),
            "last_run_status": reason,
            "next_run_at": _iso_utc(next_run) if next_run else "",
        }
        if next_run is None and str(row.get("schedule_kind")) == "once":
            fields["enabled"] = 0
        get_db().update_scheduled_task(str(row["id"]), **fields)

    def _prepare_run(self, row: dict[str, Any], *, manual: bool, initial_status: str = "running") -> dict[str, Any]:
        task_id = str(row["id"])
        started_at = _iso_utc(_utc_now())
        title = str(row.get("title") or "Scheduled task").strip() or "Scheduled task"
        if int(row.get("reuse_conversation") or 0):
            conversation_id = f"sched_{task_id[:16]}_shared"[:64]
            conversation_title = f"Scheduled: {title}"
        else:
            suffix = uuid.uuid4().hex[:8] if manual else datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            conversation_id = f"sched_{task_id[:16]}_{suffix}"[:64]
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            conversation_title = f"Scheduled: {title} - {timestamp}"
        get_db().create_conversation(conversation_id, conversation_title)
        idempotency_source = uuid.uuid4().hex if manual else str(row.get("next_run_at") or started_at)
        run_id = get_db().create_scheduled_task_run(
            task_id=task_id,
            conversation_id=conversation_id,
            started_at=started_at,
            idempotency_key=f"{task_id}:{idempotency_source}",
            lease_owner=self._lease_owner,
            lease_expires_at=self._lease_expires_at(),
            status=initial_status,
        )
        return {
            "run_id": run_id,
            "conversation_id": conversation_id,
            "started_at": started_at,
            "manual": manual,
        }

    async def _run_prepared_task(self, row: dict[str, Any], run: dict[str, Any]) -> None:
        task_id = str(row["id"])
        run_id = int(run["run_id"])
        conversation_id = str(run["conversation_id"])
        final_text_parts: list[str] = []
        final_text_chars = 0
        final_text_truncated = False
        done_summary = ""
        error_text = ""
        status = "succeeded"

        try:
            get_db().update_scheduled_task_run(
                run_id,
                status="running",
                lease_owner=self._lease_owner,
                lease_expires_at=self._lease_expires_at(),
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
            error_text = "cancelled"
            raise
        except Exception as exc:
            logger.exception("scheduled-tasks: task %s failed", task_id)
            status = "failed"
            error_text = str(exc)
        finally:
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
        status: str,
        final_text: str,
        error: str,
    ) -> None:
        now = _utc_now()
        now_iso = _iso_utc(now)
        next_run = self.compute_next_run(row, after=now)
        failures = int(row.get("consecutive_failures") or 0)
        if status == "succeeded":
            failures = 0
        elif status == "failed":
            failures += 1

        enabled = int(row.get("enabled") or 0)
        stored_status = status
        last_status = {
            "succeeded": "ok",
            "failed": "error",
            "cancelled": "cancelled",
            "skipped": "skipped",
        }.get(status, status)
        if failures >= _MAX_CONSECUTIVE_FAILURES:
            enabled = 0
            stored_status = "dead_letter"
            last_status = "dead_letter"
        elif next_run is None and str(row.get("schedule_kind")) == "once":
            enabled = 0

        get_db().update_scheduled_task_run(
            run_id,
            finished_at=now_iso,
            status=stored_status,
            final_text=final_text,
            error=error,
            clear_lease=True,
        )

        get_db().update_scheduled_task(
            str(row["id"]),
            last_run_at=now_iso,
            last_run_status=last_status,
            last_run_conversation_id=conversation_id,
            next_run_at=_iso_utc(next_run) if next_run else "",
            consecutive_failures=failures,
            enabled=enabled,
        )
        publish_ui_event(
            "scheduled_tasks.changed",
            {"task_id": str(row["id"]), "action": "run_finished", "status": last_status},
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
            await self._notify_telegram(row, final_text=final_text, status=status, error=error)

    async def _notify_telegram(
        self,
        row: dict[str, Any],
        *,
        final_text: str,
        status: str,
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
        title = str(row.get("title") or "Scheduled task").strip()
        body = final_text.strip() if status == "ok" else f"Scheduled task failed: {error or 'unknown error'}"
        text = simplify_telegram_text(f"{title}\n\n{body}".strip())
        chat_id = str(row.get("telegram_chat_id") or "").strip()
        for chunk in split_telegram_message(text):
            result = bot.send_message(chat_id=chat_id, text=chunk)
            if inspect.isawaitable(result):
                await result

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
            from croniter import croniter

            local_after = after_utc.astimezone(_zoneinfo(tz_name))
            next_local = croniter(expr, local_after).get_next(datetime)
            if next_local.tzinfo is None:
                next_local = next_local.replace(tzinfo=_zoneinfo(tz_name))
            return next_local.astimezone(timezone.utc)

        if kind == "interval":
            seconds = int(task_row.get("interval_seconds") or 0)
            if seconds <= 0:
                return None
            base = _parse_datetime(str(task_row.get("last_run_at") or ""), tz_name=tz_name) or after_utc
            next_run = base + timedelta(seconds=seconds)
            while next_run <= after_utc:
                next_run += timedelta(seconds=seconds)
            return next_run.astimezone(timezone.utc)

        if kind == "once":
            run_at = _parse_datetime(str(task_row.get("run_at") or ""), tz_name=tz_name)
            if run_at is None or run_at <= after_utc:
                return None
            return run_at.astimezone(timezone.utc)

        return None

