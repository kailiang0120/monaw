import asyncio
import json
import sqlite3
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent import run_context
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


def test_database_migrates_legacy_scheduled_task_runs_table(tmp_path):
    db_path = tmp_path / "agent.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE scheduled_task_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'running',
            final_text TEXT DEFAULT '',
            error TEXT DEFAULT ''
        );
        """
    )
    conn.close()

    db = Database(db_path)
    db.init_db()

    columns = {
        row["name"]
        for row in db.fetchall("PRAGMA table_info(scheduled_task_runs)")
    }
    indexes = {
        row["name"]
        for row in db.fetchall("SELECT name FROM sqlite_master WHERE type = 'index'")
    }

    assert {"idempotency_key", "lease_owner", "lease_expires_at"}.issubset(columns)
    assert "idx_scheduled_task_runs_idempotency" in indexes


def test_scheduled_task_create_tool_creates_cron_task(tmp_path, monkeypatch):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    monkeypatch.setattr(scheduling_tools, "get_db", lambda: db)
    monkeypatch.setattr(scheduling_tools, "get_scheduled_task_service", lambda: None)

    token = run_context.set_current_principal_id("desktop-user")
    try:
        payload = json.loads(
            scheduling_tools.scheduled_task_create(
                title="Morning report",
                prompt="Summarize overnight events",
                schedule_kind="cron",
                cron_expr="0 8 * * 1-5",
                timezone_name="Asia/Kuala_Lumpur",
            )
        )
    finally:
        run_context.reset_current_principal_id(token)

    task = db.get_scheduled_task(payload["task"]["id"])
    assert payload["status"] == "ok"
    assert task is not None
    assert task["schedule_kind"] == "cron"
    assert task["cron_expr"] == "0 8 * * 1-5"
    assert task["owner_principal_id"] == "desktop-user"
    assert task["permission_profile_id"] == "scheduled-task:desktop-user:restricted"
    snapshot = json.loads(task["permission_profile_snapshot"])
    assert snapshot["interactive"] is False
    assert snapshot["profile"] == "scheduled-restricted"
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
    calls = []

    async def fake_runner(**kwargs):
        calls.append(kwargs)
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
    assert runs[0]["status"] == "succeeded"
    assert runs[0]["final_text"] == "Done"
    assert updated is not None
    assert updated["last_run_status"] == "ok"
    assert calls[0]["execution_source"] == "scheduled"
    assert calls[0]["control_session_id"] == "scheduler"
    assert calls[0]["principal_id"] == "scheduled-task:task-1"
    assert calls[0]["permission_profile_id"] == "scheduled-task:task-1:restricted"
    assert calls[0]["interactive"] is False


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


def test_scheduled_task_output_is_truncated_before_storage(tmp_path, monkeypatch):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    task = db.create_scheduled_task(
        {
            "id": "task-1",
            "title": "Large",
            "prompt": "Say a lot",
            "schedule_kind": "interval",
            "interval_seconds": 3600,
            "next_run_at": "2026-05-08T10:00:00+00:00",
        }
    )
    monkeypatch.setattr(scheduler_module, "get_db", lambda: db)

    async def fake_runner(**kwargs):
        yield {"event": "token", "data": {"content": "x" * 25_000}}
        yield {"event": "done", "data": {"status": "complete"}}

    async def run():
        service = ScheduledTaskService(
            settings_factory=lambda: object(),
            runner=fake_runner,
            tick_interval=999,
        )
        await service.run_now(task["id"])
        await asyncio.wait_for(service._inflight[task["id"]][0], timeout=2)

    asyncio.run(run())

    final_text = db.list_scheduled_task_runs("task-1")[0]["final_text"]
    assert len(final_text) <= 20_000 + len(scheduler_module._TRUNCATED_OUTPUT_SUFFIX)
    assert final_text.endswith(scheduler_module._TRUNCATED_OUTPUT_SUFFIX)


def test_scheduled_task_enters_dead_letter_after_max_failures(tmp_path, monkeypatch):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    task = db.create_scheduled_task(
        {
            "id": "task-1",
            "title": "Failing",
            "prompt": "Fail",
            "schedule_kind": "interval",
            "interval_seconds": 3600,
            "consecutive_failures": 4,
            "next_run_at": "2026-05-08T10:00:00+00:00",
        }
    )
    monkeypatch.setattr(scheduler_module, "get_db", lambda: db)

    async def fake_runner(**kwargs):
        yield {"event": "error", "data": {"message": "boom"}}

    async def run():
        service = ScheduledTaskService(
            settings_factory=lambda: object(),
            runner=fake_runner,
            tick_interval=999,
        )
        await service.run_now(task["id"])
        await asyncio.wait_for(service._inflight[task["id"]][0], timeout=2)

    asyncio.run(run())

    run_row = db.list_scheduled_task_runs("task-1")[0]
    task_row = db.get_scheduled_task("task-1")
    assert run_row["status"] == "dead_letter"
    assert task_row is not None
    assert task_row["enabled"] == 0
    assert task_row["last_run_status"] == "dead_letter"
    assert task_row["consecutive_failures"] == 5




def test_scheduled_task_runs_store_leases_and_idempotency_keys(tmp_path):
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

    run_id = db.create_scheduled_task_run(
        task_id="task-1",
        conversation_id="conv-1",
        started_at="2026-05-08T10:00:01+00:00",
        idempotency_key="task-1:2026-05-08T00:00:00+00:00",
        lease_owner="scheduler:test",
        lease_expires_at="2026-05-08T10:15:01+00:00",
    )

    row = db.list_scheduled_task_runs("task-1")[0]
    assert row["id"] == run_id
    assert row["idempotency_key"] == "task-1:2026-05-08T00:00:00+00:00"
    assert row["lease_owner"] == "scheduler:test"
    assert row["lease_expires_at"] == "2026-05-08T10:15:01+00:00"
    try:
        db.create_scheduled_task_run(
            task_id="task-1",
            conversation_id="conv-2",
            started_at="2026-05-08T10:00:02+00:00",
            idempotency_key="task-1:2026-05-08T00:00:00+00:00",
        )
    except sqlite3.IntegrityError:
        pass
    else:  # pragma: no cover - defensive failure branch
        raise AssertionError("duplicate idempotency key was accepted")


def test_recover_expired_scheduled_task_runs_marks_abandoned_runs(tmp_path):
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
    db.create_scheduled_task_run(
        task_id="task-1",
        conversation_id="conv-1",
        started_at="2026-05-08T10:00:01+00:00",
        lease_owner="scheduler:old",
        lease_expires_at="2026-05-08T10:15:01+00:00",
    )

    recovered = db.recover_expired_scheduled_task_runs("2026-05-08T10:16:00+00:00")

    row = db.list_scheduled_task_runs("task-1")[0]
    assert recovered == 1
    assert row["status"] == "abandoned"
    assert row["finished_at"] == "2026-05-08T10:16:00+00:00"
    assert row["error"] == "scheduler lease expired"

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
        prepared = service._prepare_run(row, manual=False, initial_status="queued")
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
    assert db.list_scheduled_task_runs("task-1")[0]["status"] == "cancelled"




def test_scheduler_start_recovers_expired_run_leases(tmp_path, monkeypatch):
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
    db.create_scheduled_task_run(
        task_id="task-1",
        conversation_id="conv-1",
        started_at="2026-05-08T10:00:01+00:00",
        lease_owner="scheduler:old",
        lease_expires_at="2000-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr(scheduler_module, "get_db", lambda: db)

    async def run():
        service = ScheduledTaskService(settings_factory=lambda: object(), tick_interval=999)
        await service.start()
        await service.stop()

    asyncio.run(run())

    assert db.list_scheduled_task_runs("task-1")[0]["status"] == "abandoned"


def test_scheduler_global_concurrency_limit_skips_due_work(tmp_path, monkeypatch):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    db.create_scheduled_task(
        {
            "id": "task-1",
            "title": "One",
            "prompt": "Say one",
            "schedule_kind": "interval",
            "interval_seconds": 60,
            "next_run_at": "2026-05-08T10:00:00+00:00",
        }
    )
    db.create_scheduled_task(
        {
            "id": "task-2",
            "title": "Two",
            "prompt": "Say two",
            "schedule_kind": "interval",
            "interval_seconds": 60,
            "next_run_at": "2026-05-08T10:00:00+00:00",
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
            max_global_concurrency=1,
        )
        await service._fire_due_tasks()
        await asyncio.gather(
            *(task for tasks in service._inflight.values() for task in tasks),
            return_exceptions=True,
        )

    asyncio.run(run())

    statuses = {
        task["id"]: task["last_run_status"]
        for task in db.list_scheduled_tasks()
    }
    assert sorted(statuses.values()) == ["global_concurrency_limit", "ok"]

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



def test_scheduler_uses_persisted_permission_snapshot_identity(tmp_path, monkeypatch):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    task = db.create_scheduled_task(
        {
            "id": "task-owned",
            "title": "Owned",
            "prompt": "Say done",
            "schedule_kind": "once",
            "run_at": "2026-05-08T10:00:00+00:00",
            "next_run_at": "2026-05-08T10:00:00+00:00",
            "owner_principal_id": "owner-123",
            "permission_profile_id": "snapshot-profile-123",
            "permission_profile_snapshot": json.dumps({"profile": "scheduled-restricted"}),
        }
    )
    monkeypatch.setattr(scheduler_module, "get_db", lambda: db)
    calls = []

    async def fake_runner(**kwargs):
        calls.append(kwargs)
        yield {"event": "done", "data": {"summary": "Done", "status": "complete"}}

    async def run():
        service = ScheduledTaskService(settings_factory=lambda: object(), runner=fake_runner, tick_interval=999)
        await service.run_now(task["id"])
        await asyncio.wait_for(service._inflight[task["id"]][0], timeout=2)

    asyncio.run(run())

    assert calls[0]["principal_id"] == "owner-123"
    assert calls[0]["permission_profile_id"] == "snapshot-profile-123"
    assert calls[0]["interactive"] is False
