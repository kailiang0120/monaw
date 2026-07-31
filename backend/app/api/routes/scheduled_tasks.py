from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agent.database import get_db
from app.agent.scheduler import ScheduledTaskService, get_scheduled_task_service
from app.agent.ui_events import publish_ui_event
from app.integrations.telegram.session import TelegramSessionStore

router = APIRouter(prefix="/scheduled-tasks")


def _to_camel(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        serialize_by_alias=True,
    )


class ScheduledTaskBase(CamelModel):
    title: str = Field("", max_length=200)
    prompt: str = Field("", max_length=20000)
    schedule_kind: Literal["cron", "interval", "once"] = "interval"
    cron_expr: str = Field("", max_length=120)
    interval_seconds: int = Field(0, ge=0)
    run_at: str = Field("", max_length=80)
    timezone: str = Field("UTC", max_length=80)
    enabled: bool = True
    overlap_policy: Literal["skip", "queue", "cancel_previous"] = "skip"
    notify_telegram: bool = False
    telegram_chat_id: str = Field("", max_length=80)
    reuse_conversation: bool = False

    @model_validator(mode="after")
    def validate_schedule(self) -> "ScheduledTaskBase":
        if self.schedule_kind == "cron" and not self.cron_expr.strip():
            raise ValueError("cronExpr is required for cron schedules")
        if self.schedule_kind == "interval" and self.interval_seconds <= 0:
            raise ValueError("intervalSeconds must be greater than zero")
        if self.schedule_kind == "once" and not self.run_at.strip():
            raise ValueError("runAt is required for one-time schedules")
        return self


class ScheduledTaskCreate(ScheduledTaskBase):
    title: str = Field(..., min_length=1, max_length=200)
    prompt: str = Field(..., min_length=1, max_length=20000)


class ScheduledTaskUpdate(CamelModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    prompt: str | None = Field(None, min_length=1, max_length=20000)
    schedule_kind: Literal["cron", "interval", "once"] | None = None
    cron_expr: str | None = Field(None, max_length=120)
    interval_seconds: int | None = Field(None, ge=0)
    run_at: str | None = Field(None, max_length=80)
    timezone: str | None = Field(None, max_length=80)
    enabled: bool | None = None
    overlap_policy: Literal["skip", "queue", "cancel_previous"] | None = None
    notify_telegram: bool | None = None
    telegram_chat_id: str | None = Field(None, max_length=80)
    reuse_conversation: bool | None = None


class ScheduledTaskOut(ScheduledTaskBase):
    id: str
    last_run_at: str = ""
    last_run_status: str = ""
    last_run_conversation_id: str = ""
    next_run_at: str = ""
    consecutive_failures: int = 0
    running: bool = False
    created_at: str
    updated_at: str


class ScheduledTaskRunOut(CamelModel):
    id: int
    task_id: str
    conversation_id: str
    started_at: str
    finished_at: str = ""
    status: str
    final_text: str = ""
    error: str = ""


class SchedulePreviewRequest(CamelModel):
    schedule_kind: Literal["cron", "interval", "once"]
    cron_expr: str = ""
    interval_seconds: int = 0
    run_at: str = ""
    timezone: str = "UTC"


class SchedulePreviewResponse(CamelModel):
    next: list[str]


class RunNowResponse(CamelModel):
    run_id: int
    conversation_id: str


class TelegramChatTarget(CamelModel):
    id: str
    label: str


class TelegramChatsResponse(CamelModel):
    chat_ids: list[str]
    chats: list[TelegramChatTarget] = Field(default_factory=list)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalize_task_row(row: dict[str, Any], *, running: bool = False) -> dict[str, Any]:
    normalized = dict(row)
    normalized["interval_seconds"] = int(normalized.get("interval_seconds") or 0)
    normalized["consecutive_failures"] = int(normalized.get("consecutive_failures") or 0)
    normalized["running"] = running
    return normalized


def _task_fields(model: ScheduledTaskBase | ScheduledTaskUpdate) -> dict[str, Any]:
    raw = model.model_dump(by_alias=False, exclude_none=True)
    fields: dict[str, Any] = {}
    for key, value in raw.items():
        if key in {"enabled", "notify_telegram", "reuse_conversation"}:
            fields[key] = 1 if value else 0
        elif isinstance(value, str):
            fields[key] = value.strip()
        else:
            fields[key] = value
    return fields


def _compute_next_run_or_400(fields: dict[str, Any], current: dict[str, Any] | None = None) -> str:
    row = {**(current or {}), **fields}
    if not int(row.get("enabled") if row.get("enabled") is not None else 1):
        return ""
    try:
        next_run = ScheduledTaskService.compute_next_run(row, after=datetime.now(timezone.utc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid schedule: {exc}") from exc
    if next_run is None:
        raise HTTPException(status_code=400, detail="Schedule never fires")
    return next_run.replace(microsecond=0).isoformat()


def _initial_next_run_or_400(fields: dict[str, Any]) -> str:
    if not int(fields.get("enabled") if fields.get("enabled") is not None else 1):
        return ""
    _compute_next_run_or_400(fields)
    if fields.get("schedule_kind") == "interval":
        seconds = max(1, int(fields.get("interval_seconds") or 0))
        delay = min(60, seconds)
        return (datetime.now(timezone.utc) + timedelta(seconds=delay)).replace(microsecond=0).isoformat()
    return _compute_next_run_or_400(fields)


def _validate_merged_task(fields: dict[str, Any], current: dict[str, Any]) -> None:
    try:
        ScheduledTaskBase.model_validate({**current, **fields})
    except ValidationError as exc:
        detail = exc.errors()[0].get("msg") if exc.errors() else "Invalid scheduled task"
        raise HTTPException(status_code=400, detail=detail) from exc


@router.get("", response_model=list[ScheduledTaskOut])
async def list_scheduled_tasks():
    db = get_db()
    running_ids = db.list_running_scheduled_task_ids()
    return [_normalize_task_row(row, running=str(row["id"]) in running_ids) for row in db.list_scheduled_tasks()]


@router.post("", response_model=ScheduledTaskOut)
async def create_scheduled_task(body: ScheduledTaskCreate):
    fields = _task_fields(body)
    fields["id"] = uuid.uuid4().hex
    fields["next_run_at"] = _initial_next_run_or_400(fields)
    task = get_db().create_scheduled_task(fields)
    service = get_scheduled_task_service()
    if service is not None:
        await service.refresh(str(task["id"]))
    publish_ui_event("scheduled_tasks.changed", {"task_id": str(task["id"]), "action": "created"})
    return _normalize_task_row(task)


@router.patch("/{task_id}", response_model=ScheduledTaskOut)
async def update_scheduled_task(task_id: str, body: ScheduledTaskUpdate):
    db = get_db()
    current = db.get_scheduled_task(task_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Scheduled task not found")

    fields = _task_fields(body)
    _validate_merged_task(fields, current)
    next_input_keys = {
        "schedule_kind",
        "cron_expr",
        "interval_seconds",
        "run_at",
        "timezone",
        "enabled",
    }
    if fields.keys() & next_input_keys:
        fields["next_run_at"] = _compute_next_run_or_400(fields, current)
    updated = db.update_scheduled_task(task_id, **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    service = get_scheduled_task_service()
    if service is not None:
        await service.refresh(task_id)
    publish_ui_event("scheduled_tasks.changed", {"task_id": task_id, "action": "updated"})
    return _normalize_task_row(
        updated,
        running=task_id in db.list_running_scheduled_task_ids(),
    )


@router.delete("/{task_id}", status_code=204)
async def delete_scheduled_task(task_id: str):
    if not get_db().delete_scheduled_task(task_id):
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    publish_ui_event("scheduled_tasks.changed", {"task_id": task_id, "action": "deleted"})
    return Response(status_code=204)


@router.post("/{task_id}/run", response_model=RunNowResponse)
async def run_scheduled_task_now(task_id: str):
    if get_db().get_scheduled_task(task_id) is None:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    service = get_scheduled_task_service()
    if service is None:
        raise HTTPException(status_code=503, detail="Scheduled task service is not running")
    try:
        result = await service.run_now(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Scheduled task not found") from None
    publish_ui_event("scheduled_tasks.changed", {"task_id": task_id, "action": "run_now"})
    publish_ui_event(
        "conversation.changed",
        {"conversation_id": result.get("conversation_id", ""), "action": "scheduled_run"},
    )
    return result


@router.get("/{task_id}/runs", response_model=list[ScheduledTaskRunOut])
async def list_scheduled_task_runs(task_id: str, limit: int = Query(20, ge=1, le=100)):
    if get_db().get_scheduled_task(task_id) is None:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    return get_db().list_scheduled_task_runs(task_id, limit=limit)


@router.post("/preview", response_model=SchedulePreviewResponse)
async def preview_schedule(body: SchedulePreviewRequest):
    row = _task_fields(
        ScheduledTaskBase(
            title="preview",
            prompt="preview",
            schedule_kind=body.schedule_kind,
            cron_expr=body.cron_expr,
            interval_seconds=body.interval_seconds,
            run_at=body.run_at,
            timezone=body.timezone,
        )
    )
    after = datetime.now(timezone.utc)
    next_values: list[str] = []
    try:
        for _ in range(5):
            next_run = ScheduledTaskService.compute_next_run(row, after=after)
            if next_run is None:
                break
            next_values.append(next_run.replace(microsecond=0).isoformat())
            row["last_run_at"] = next_run.replace(microsecond=0).isoformat()
            after = next_run
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid schedule: {exc}") from exc
    return {"next": next_values}


@router.get("/telegram-chats", response_model=TelegramChatsResponse)
async def list_telegram_chats():
    store = TelegramSessionStore()
    try:
        sessions = store.list_chat_sessions()
    except Exception:
        sessions = []
    db = get_db()
    chats: list[dict[str, str]] = []
    for chat_id, conversation_id in sessions:
        conversation = db.get_conversation(conversation_id)
        label = str((conversation or {}).get("title") or "").strip()
        if not label or label == "Untitled":
            label = f"Telegram - {chat_id}"
        chats.append({"id": chat_id, "label": label})
    return {"chat_ids": [chat["id"] for chat in chats], "chats": chats}
