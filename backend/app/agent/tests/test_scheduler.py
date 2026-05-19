import asyncio
import json
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.database import Database
from app.agent import scheduler as scheduler_module
from app.agent.scheduler import ScheduledTaskService
from app.api.routes import scheduled_tasks as scheduled_tasks_route
from app.skills.scheduling import tools as scheduling_tools


def test_compute_next_run_for_interval_skips_past_times():
    after = datetime(2026, 5, 8, 10, 5, tzinfo=timezone.utc)
    row = {
        "schedule_kind": "interval",
        "interval_seconds": 60,
        "last_run_at": "2026-05-08T10:00:00+00:00",
    }

    next_run = ScheduledTaskService.compute_next_run(row, after=after)

    assert next_run == datetime(2026, 5, 8, 10, 6, tzinfo=timezone.utc)


def test_database_scheduled_task_crud(tmp_path):
    db = Database(tmp_path / "agent.db")
    db.init_db()

    task = db.create_scheduled_task(
        {
            "id": "task-1",
            "title": "Morning",
            "prompt": "Summarize",
            "schedule_kind": "interval",
            "interval_seconds": 3600,
            "next_run_at": "2026-05-08T10:00:00+00:00",
        }
    )

    assert task["title"] == "Morning"
    assert task["reuse_conversation"] == 0
    assert db.list_due_scheduled_tasks("2026-05-08T10:00:00+00:00")[0]["id"] == "task-1"

    updated = db.update_scheduled_task("task-1", enabled=0)

    assert updated is not None
    assert updated["enabled"] == 0
    assert db.delete_scheduled_task("task-1") is True


def test_scheduled_task_create_tool_creates_cron_task(tmp_path, monkeypatch):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    monkeypatch.setattr(scheduling_tools, "get_db", lambda: db)
    monkeypatch.setattr(scheduling_tools, "get_scheduled_task_service", lambda: None)

    payload = json.loads(
        scheduling_tools.scheduled_task_create(
            title="Morning report",
            prompt="Summarize overnight events",
            schedule_kind="cron",
            cron_expr="0 8 * * 1-5",
            timezone_name="Asia/Kuala_Lumpur",
        )
    )

    task = db.get_scheduled_task(payload["task"]["id"])
    assert payload["status"] == "ok"
    assert task is not None
    assert task["schedule_kind"] == "cron"
    assert task["cron_expr"] == "0 8 * * 1-5"
    assert task["next_run_at"]


def test_run_now_creates_conversation_and_records_success(tmp_path, monkeypatch):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    task = db.create_scheduled_task(
        {
            "id": "task-1",
            "title": "Manual",
            "prompt": "Say done",
            "schedule_kind": "interval",
            "interval_seconds": 3600,
            "next_run_at": "2026-05-08T10:00:00+00:00",
        }
    )
    monkeypatch.setattr(scheduler_module, "get_db", lambda: db)

    async def fake_runner(**kwargs):
        yield {"event": "token", "data": {"content": "Done"}}
        yield {"event": "done", "data": {"summary": "Done", "status": "complete"}}

    async def run():
        service = ScheduledTaskService(
            settings_factory=lambda: object(),
            runner=fake_runner,
            tick_interval=999,
        )
        result = await service.run_now(task["id"])
        await asyncio.wait_for(service._inflight[task["id"]][0], timeout=2)
        return result

    result = asyncio.run(run())
    runs = db.list_scheduled_task_runs("task-1")
    updated = db.get_scheduled_task("task-1")

    assert result["conversation_id"].startswith("sched_")
    assert runs[0]["status"] == "ok"
    assert runs[0]["final_text"] == "Done"
    assert updated is not None
    assert updated["last_run_status"] == "ok"


def test_reuse_conversation_task_records_runs_in_same_conversation(tmp_path, monkeypatch):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    task = db.create_scheduled_task(
        {
            "id": "task-1",
            "title": "Reusable",
            "prompt": "Say done",
            "schedule_kind": "interval",
            "interval_seconds": 3600,
            "reuse_conversation": 1,
            "next_run_at": "2026-05-08T10:00:00+00:00",
        }
    )
    monkeypatch.setattr(scheduler_module, "get_db", lambda: db)

    async def fake_runner(**kwargs):
        yield {"event": "done", "data": {"summary": "Done", "status": "complete"}}

    async def run():
        service = ScheduledTaskService(
            settings_factory=lambda: object(),
            runner=fake_runner,
            tick_interval=999,
        )
        first = await service.run_now(task["id"])
        await asyncio.wait_for(
            asyncio.gather(*(item for tasks in service._inflight.values() for item in tasks)),
            timeout=2,
        )
        second = await service.run_now(task["id"])
        await asyncio.wait_for(
            asyncio.gather(*(item for tasks in service._inflight.values() for item in tasks)),
            timeout=2,
        )
        return first, second

    first, second = asyncio.run(run())
    runs = db.list_scheduled_task_runs("task-1")

    assert first["conversation_id"] == second["conversation_id"]
    assert first["conversation_id"] == "sched_task-1_shared"
    assert {run["conversation_id"] for run in runs} == {first["conversation_id"]}


def test_claim_due_scheduled_task_is_atomic(tmp_path):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    db.create_scheduled_task(
        {
            "id": "task-1",
            "title": "Once",
            "prompt": "Say done",
            "schedule_kind": "once",
            "run_at": "2026-05-08T00:00:00+00:00",
            "next_run_at": "2026-05-08T00:00:00+00:00",
        }
    )

    first = db.claim_due_scheduled_task(
        "task-1",
        expected_next_run_at="2026-05-08T00:00:00+00:00",
        now_iso="2026-05-08T10:00:01+00:00",
    )
    second = db.claim_due_scheduled_task(
        "task-1",
        expected_next_run_at="2026-05-08T00:00:00+00:00",
        now_iso="2026-05-08T10:00:01+00:00",
    )

    assert first is True
    assert second is False
    assert db.list_due_scheduled_tasks("2026-05-08T10:00:02+00:00") == []


def test_due_once_task_only_creates_one_run_when_polled_twice(tmp_path, monkeypatch):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    db.create_scheduled_task(
        {
            "id": "task-1",
            "title": "Once",
            "prompt": "Say done",
            "schedule_kind": "once",
            "run_at": "2026-05-08T00:00:00+00:00",
            "next_run_at": "2026-05-08T00:00:00+00:00",
        }
    )
    monkeypatch.setattr(scheduler_module, "get_db", lambda: db)

    async def fake_runner(**kwargs):
        await asyncio.sleep(0.05)
        yield {"event": "done", "data": {"summary": "Done", "status": "complete"}}

    async def run():
        service = ScheduledTaskService(
            settings_factory=lambda: object(),
            runner=fake_runner,
            tick_interval=999,
        )
        await service._fire_due_tasks()
        await service._fire_due_tasks()
        await asyncio.gather(
            *(task for tasks in service._inflight.values() for task in tasks),
            return_exceptions=True,
        )

    asyncio.run(run())

    assert len(db.list_scheduled_task_runs("task-1")) == 1
    assert len([c for c in db.list_conversations() if c["id"].startswith("sched_")]) == 1


def test_cancelled_queued_run_does_not_start_after_previous(tmp_path, monkeypatch):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    row = db.create_scheduled_task(
        {
            "id": "task-1",
            "title": "Queued",
            "prompt": "Say done",
            "schedule_kind": "interval",
            "interval_seconds": 60,
            "overlap_policy": "queue",
            "next_run_at": "2026-05-08T10:00:00+00:00",
        }
    )
    monkeypatch.setattr(scheduler_module, "get_db", lambda: db)
    calls = {"runner": 0}

    async def fake_runner(**kwargs):
        calls["runner"] += 1
        yield {"event": "done", "data": {"summary": "Done", "status": "complete"}}

    async def run():
        service = ScheduledTaskService(
            settings_factory=lambda: object(),
            runner=fake_runner,
            tick_interval=999,
        )
        previous = asyncio.create_task(asyncio.sleep(10))
        prepared = service._prepare_run(row, manual=False)
        queued = asyncio.create_task(
            service._run_after_previous("task-1", previous, row, prepared)
        )
        await asyncio.sleep(0)
        queued.cancel()
        try:
            await queued
        except asyncio.CancelledError:
            pass
        assert not previous.cancelled()
        previous.cancel()
        await asyncio.gather(previous, return_exceptions=True)

    asyncio.run(run())

    assert calls["runner"] == 0
    assert db.list_scheduled_task_runs("task-1")[0]["status"] == "skipped"


def test_scheduled_tasks_api_emits_camel_case_and_marks_running(tmp_path, monkeypatch):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    db.create_scheduled_task(
        {
            "id": "task-1",
            "title": "Morning",
            "prompt": "Summarize",
            "schedule_kind": "interval",
            "interval_seconds": 3600,
            "next_run_at": "2026-05-08T10:00:00+00:00",
        }
    )
    db.create_scheduled_task_run(
        task_id="task-1",
        conversation_id="conv-1",
        started_at="2026-05-08T10:00:00+00:00",
    )
    monkeypatch.setattr(scheduled_tasks_route, "get_db", lambda: db)

    app = FastAPI()
    app.include_router(scheduled_tasks_route.router, prefix="/api")
    client = TestClient(app)

    response = client.get("/api/scheduled-tasks")

    assert response.status_code == 200
    payload = response.json()[0]
    assert payload["scheduleKind"] == "interval"
    assert payload["intervalSeconds"] == 3600
    assert payload["nextRunAt"] == "2026-05-08T10:00:00+00:00"
    assert payload["running"] is True
    assert "schedule_kind" not in payload


def test_scheduled_tasks_patch_rejects_invalid_merged_schedule(tmp_path, monkeypatch):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    db.create_scheduled_task(
        {
            "id": "task-1",
            "title": "Morning",
            "prompt": "Summarize",
            "schedule_kind": "cron",
            "cron_expr": "0 8 * * 1-5",
            "next_run_at": "2026-05-11T08:00:00+00:00",
        }
    )
    monkeypatch.setattr(scheduled_tasks_route, "get_db", lambda: db)
    monkeypatch.setattr(scheduled_tasks_route, "get_scheduled_task_service", lambda: None)

    app = FastAPI()
    app.include_router(scheduled_tasks_route.router, prefix="/api")
    client = TestClient(app)

    response = client.patch("/api/scheduled-tasks/task-1", json={"cronExpr": ""})

    assert response.status_code == 400
    assert "cronExpr" in response.json()["detail"]


def test_scheduled_tasks_create_interval_runs_promptly(tmp_path, monkeypatch):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    monkeypatch.setattr(scheduled_tasks_route, "get_db", lambda: db)
    monkeypatch.setattr(scheduled_tasks_route, "get_scheduled_task_service", lambda: None)

    app = FastAPI()
    app.include_router(scheduled_tasks_route.router, prefix="/api")
    client = TestClient(app)

    before = datetime.now(timezone.utc)
    response = client.post(
        "/api/scheduled-tasks",
        json={
            "title": "Hourly",
            "prompt": "Summarize",
            "scheduleKind": "interval",
            "intervalSeconds": 3600,
            "enabled": True,
        },
    )

    assert response.status_code == 200
    next_run_at = datetime.fromisoformat(response.json()["nextRunAt"])
    assert 0 <= (next_run_at - before).total_seconds() <= 60


def test_scheduled_tasks_telegram_chats_return_labels(tmp_path, monkeypatch):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    db.create_conversation("telegram_123", "Telegram - Ang")
    monkeypatch.setattr(scheduled_tasks_route, "get_db", lambda: db)

    class FakeTelegramSessionStore:
        def list_chat_sessions(self):
            return [("123", "telegram_123")]

    monkeypatch.setattr(scheduled_tasks_route, "TelegramSessionStore", FakeTelegramSessionStore)

    app = FastAPI()
    app.include_router(scheduled_tasks_route.router, prefix="/api")
    client = TestClient(app)

    response = client.get("/api/scheduled-tasks/telegram-chats")

    assert response.status_code == 200
    assert response.json()["chats"] == [{"id": "123", "label": "Telegram - Ang"}]
