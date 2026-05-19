from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.agent.database import get_db
from app.agent.scheduler import ScheduledTaskService, get_scheduled_task_service


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _compute_next_run(row: dict[str, Any], *, initial: bool = False) -> str:
    if not int(row.get("enabled") if row.get("enabled") is not None else 1):
        return ""
    if row.get("schedule_kind") == "interval" and initial:
        seconds = max(1, int(row.get("interval_seconds") or 0))
        return _iso_utc(_now() + timedelta(seconds=min(60, seconds)))
    next_run = ScheduledTaskService.compute_next_run(row, after=_now())
    if next_run is None:
        raise ValueError("Schedule never fires")
    return _iso_utc(next_run)


def _normalize_task(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    for key in ("enabled", "notify_telegram", "reuse_conversation"):
        payload[key] = bool(payload.get(key))
    for key in ("interval_seconds", "consecutive_failures"):
        payload[key] = int(payload.get(key) or 0)
    return payload


def _validate_cron(cron_expr: str) -> None:
    if not cron_expr.strip():
        raise ValueError("cron_expr is required for cron schedules")
    from croniter import croniter

    if not croniter.is_valid(cron_expr):
        raise ValueError(f"Invalid cron expression: {cron_expr}")


def scheduled_task_create(
    title: str,
    prompt: str,
    schedule_kind: str = "cron",
    cron_expr: str = "",
    interval_seconds: int = 0,
    run_at: str = "",
    timezone_name: str = "UTC",
    enabled: bool = True,
    overlap_policy: str = "skip",
    notify_telegram: bool = False,
    telegram_chat_id: str = "",
    reuse_conversation: bool = False,
) -> str:
    title = title.strip()
    prompt = prompt.strip()
    schedule_kind = schedule_kind.strip().lower()
    overlap_policy = overlap_policy.strip().lower() or "skip"
    if not title:
        return _json({"status": "error", "error": "title is required"})
    if not prompt:
        return _json({"status": "error", "error": "prompt is required"})
    if schedule_kind not in {"cron", "interval", "once"}:
        return _json({"status": "error", "error": "schedule_kind must be cron, interval, or once"})
    if overlap_policy not in {"skip", "queue", "cancel_previous"}:
        return _json({"status": "error", "error": "overlap_policy must be skip, queue, or cancel_previous"})

    try:
        if schedule_kind == "cron":
            _validate_cron(cron_expr)
            interval_seconds = 0
            run_at = ""
        elif schedule_kind == "interval":
            if int(interval_seconds or 0) <= 0:
                raise ValueError("interval_seconds must be greater than zero")
            cron_expr = ""
            run_at = ""
        else:
            if not run_at.strip():
                raise ValueError("run_at is required for one-time schedules")
            cron_expr = ""
            interval_seconds = 0

        fields = {
            "id": uuid.uuid4().hex,
            "title": title,
            "prompt": prompt,
            "schedule_kind": schedule_kind,
            "cron_expr": cron_expr.strip(),
            "interval_seconds": int(interval_seconds or 0),
            "run_at": run_at.strip(),
            "timezone": timezone_name.strip() or "UTC",
            "enabled": 1 if enabled else 0,
            "overlap_policy": overlap_policy,
            "notify_telegram": 1 if notify_telegram else 0,
            "telegram_chat_id": telegram_chat_id.strip(),
            "reuse_conversation": 1 if reuse_conversation else 0,
        }
        fields["next_run_at"] = _compute_next_run(fields, initial=True)
        task = get_db().create_scheduled_task(fields)
        service = get_scheduled_task_service()
        if service is not None:
            # The service polls SQLite; refresh preserves the explicit API contract.
            try:
                import asyncio

                loop = asyncio.get_running_loop()
                loop.create_task(service.refresh(str(task["id"])))
            except RuntimeError:
                pass
        return _json({"status": "ok", "task": _normalize_task(task)})
    except Exception as exc:
        return _json({"status": "error", "error": str(exc)})


def scheduled_task_list(limit: int = 20) -> str:
    tasks = get_db().list_scheduled_tasks()[: max(1, min(100, int(limit or 20)))]
    return _json({"status": "ok", "tasks": [_normalize_task(task) for task in tasks], "count": len(tasks)})


def scheduled_task_delete(task_id: str) -> str:
    deleted = get_db().delete_scheduled_task(task_id.strip())
    if not deleted:
        return _json({"status": "error", "error": f"Scheduled task {task_id} not found"})
    return _json({"status": "ok", "task_id": task_id})


async def scheduled_task_run_now(task_id: str) -> str:
    service = get_scheduled_task_service()
    if service is None:
        return _json({"status": "error", "error": "Scheduled task service is not running"})
    try:
        result = await service.run_now(task_id.strip())
        return _json({"status": "ok", "task_id": task_id.strip(), **result})
    except Exception as exc:
        return _json({"status": "error", "error": str(exc)})


def register_tools(registry, _settings=None) -> None:
    common = {
        "domain": "general",
        "execution_mode": "sync_stateless",
        "affinity_group": None,
        "visible_to_model": False,
        "metadata": {
            "parallel_safe": False,
            "resource_locks": ["scheduled_tasks"],
            "mutates_state": True,
            "risk_level": "low",
            "dynamic_load": True,
            "search_tags": ["schedule", "cron", "timer", "remind", "recurring", "interval", "automate"],
        },
    }
    registry.extend(
        [
            {
                **common,
                "name": "scheduled_task_create",
                "description": "Create a local scheduled agent task. Supports cron, interval, and one-time schedules.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Short task title."},
                        "prompt": {"type": "string", "description": "Prompt the agent should run when the schedule fires."},
                        "schedule_kind": {"type": "string", "enum": ["cron", "interval", "once"], "default": "cron"},
                        "cron_expr": {"type": "string", "default": "", "description": "Five-field cron expression for cron schedules."},
                        "interval_seconds": {"type": "integer", "default": 0},
                        "run_at": {"type": "string", "default": "", "description": "ISO timestamp for one-time schedules."},
                        "timezone_name": {"type": "string", "default": "UTC"},
                        "enabled": {"type": "boolean", "default": True},
                        "overlap_policy": {"type": "string", "enum": ["skip", "queue", "cancel_previous"], "default": "skip"},
                        "notify_telegram": {"type": "boolean", "default": False},
                        "telegram_chat_id": {"type": "string", "default": ""},
                        "reuse_conversation": {
                            "type": "boolean",
                            "default": False,
                            "description": "Reuse one conversation for every run of this task.",
                        },
                    },
                    "required": ["title", "prompt"],
                },
                "callable": scheduled_task_create,
            },
            {
                **common,
                "name": "scheduled_task_list",
                "description": "List configured scheduled agent tasks.",
                "parameters": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "default": 20}},
                    "required": [],
                },
                "callable": scheduled_task_list,
            },
            {
                **common,
                "name": "scheduled_task_delete",
                "description": "Delete a scheduled agent task by id.",
                "parameters": {
                    "type": "object",
                    "properties": {"task_id": {"type": "string"}},
                    "required": ["task_id"],
                },
                "callable": scheduled_task_delete,
            },
            {
                **common,
                "name": "scheduled_task_run_now",
                "description": "Run a scheduled agent task immediately by id.",
                "parameters": {
                    "type": "object",
                    "properties": {"task_id": {"type": "string"}},
                    "required": ["task_id"],
                },
                "callable": scheduled_task_run_now,
                "execution_mode": "async",
            },
        ]
    )
