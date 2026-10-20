import asyncio
import json
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent import run_context
from app.agent.database import Database, ScheduledTaskConflictError
from app.agent import scheduler as scheduler_module
from app.agent.scheduler import ScheduledTaskService
from app.api.routes import scheduled_tasks as scheduled_tasks_route
from app.skills.scheduling import tools as scheduling_tools


def _prepared_fixture(db, service, row):
    conversation_id = f"fixture-{uuid.uuid4().hex}"
    db.create_conversation(conversation_id, "Fixture")
    run_id = db.create_scheduled_task_run(
        task_id=row["id"], conversation_id=conversation_id,
        started_at=scheduler_module._iso_utc(scheduler_module._utc_now()),
        lease_owner=service._lease_owner, lease_expires_at=service._lease_expires_at(),
    )
    return {"run_id": run_id, "conversation_id": conversation_id}


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
            scheduling_tools._raw_scheduled_task_create(
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


@pytest.mark.parametrize(
    "event,expected_body,expected_status,delivery_fails",
    [
        ({"event": "done", "data": {"summary": "Report ready"}}, "Report ready", "succeeded", False),
        ({"event": "done", "data": {}}, "Scheduled task completed successfully (no output).", "succeeded", False),
        ({"event": "error", "data": {"message": "Fixture failed"}}, "Scheduled task failed: Fixture failed", "failed", False),
        ({"event": "done", "data": {"summary": "Report ready"}}, "Report ready", "succeeded", True),
    ],
)
def test_completion_delivers_canonical_outcome_without_changing_execution_result(
    tmp_path, monkeypatch, event, expected_body, expected_status, delivery_fails, caplog,
):
    from app.integrations.telegram import service as telegram_service

    db = Database(tmp_path / "agent.db")
    db.init_db()
    row = db.create_scheduled_task({
        "id": "notify", "title": "Report", "prompt": "Prepare report",
        "schedule_kind": "interval", "interval_seconds": 3600,
        "notify_telegram": 1, "telegram_chat_id": "123",
    })
    monkeypatch.setattr(scheduler_module, "get_db", lambda: db)
    monkeypatch.setattr(scheduler_module, "telegram_chat_allowed", lambda _: True)
    messages = []

    class Bot:
        async def send_message(self, **payload):
            messages.append(payload)
            if delivery_fails:
                raise RuntimeError("sentinel-transport-secret")

    monkeypatch.setattr(telegram_service, "get_telegram_bot", lambda: Bot())

    async def runner(**kwargs):
        yield event

    async def run():
        service = ScheduledTaskService(settings_factory=lambda: object(), runner=runner)
        await service.run_now(row["id"])
        await asyncio.wait_for(service._inflight[row["id"]][0], timeout=2)

    asyncio.run(run())
    assert len(messages) == 1
    assert messages[0]["chat_id"] == "123"
    assert expected_body in messages[0]["text"]
    assert db.list_scheduled_task_runs(row["id"])[0]["status"] == expected_status
    assert db.get_scheduled_task(row["id"])["last_run_status"] == (
        "ok" if expected_status == "succeeded" else "error"
    )
    assert "sentinel-transport-secret" not in caplog.text
    if delivery_fails:
        assert "notification delivery failed" in caplog.text


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


@pytest.mark.parametrize("initial_kind", ["interval", "once"])
@pytest.mark.parametrize("patch", [
    {"enabled": 0, "next_run_at": ""},
    {"schedule_kind": "interval", "interval_seconds": 7200},
    {"schedule_kind": "cron", "cron_expr": "0 9 * * *", "timezone": "Asia/Kuala_Lumpur"},
    {"schedule_kind": "once", "run_at": "2099-01-01T00:00:00+00:00"},
])
def test_completion_preserves_configuration_saved_while_running(tmp_path, monkeypatch, patch, initial_kind):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    row = db.create_scheduled_task({
        "id": "edited", "title": "Editable", "prompt": "Work",
        "schedule_kind": initial_kind, "interval_seconds": 60, "run_at": "2099-01-01T00:00:00+00:00",
    })
    monkeypatch.setattr(scheduler_module, "get_db", lambda: db)

    async def runner(**kwargs):
        db.update_scheduled_task(row["id"], **patch)
        yield {"event": "done", "data": {"summary": "Done"}}

    async def run():
        service = ScheduledTaskService(settings_factory=lambda: object(), runner=runner)
        await service.run_now(row["id"])
        await service._inflight[row["id"]][0]

    asyncio.run(run())
    current = db.get_scheduled_task(row["id"])
    assert current["config_revision"] == 1
    assert db.list_scheduled_task_runs(row["id"])[0]["config_revision"] == 0
    for key, value in patch.items():
        assert current[key] == value
    if current["enabled"]:
        after = datetime.fromisoformat(current["last_run_at"])
        expected = ScheduledTaskService.compute_next_run(current, after=after)
        assert current["next_run_at"] == scheduler_module._iso_utc(expected)
    else:
        assert current["next_run_at"] == ""


def test_completion_is_idempotent_and_old_attempt_cannot_overwrite_newer_run(tmp_path, monkeypatch):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    row = db.create_scheduled_task({
        "id": "fenced", "title": "Fenced", "prompt": "Work",
        "schedule_kind": "interval", "interval_seconds": 60,
    })
    monkeypatch.setattr(scheduler_module, "get_db", lambda: db)
    events = []
    monkeypatch.setattr(scheduler_module, "publish_ui_event", lambda *args: events.append(args))

    async def finish():
        service = ScheduledTaskService(settings_factory=lambda: object())
        old = _prepared_fixture(db, service, row)
        new = _prepared_fixture(db, service, row)
        await service._finish_run(row, run_id=new["run_id"], conversation_id=new["conversation_id"],
                                  status="succeeded", final_text="New", error="")
        snapshot = db.get_scheduled_task(row["id"])
        await service._finish_run(row, run_id=old["run_id"], conversation_id=old["conversation_id"],
                                  status="failed", final_text="", error="Old failure")
        assert db.get_scheduled_task(row["id"]) == snapshot
        event_count = len(events)
        await service._finish_run(row, run_id=new["run_id"], conversation_id=new["conversation_id"],
                                  status="failed", final_text="", error="Duplicate")
        assert len(events) == event_count
        assert db.get_scheduled_task(row["id"]) == snapshot
        runs = db.list_scheduled_task_runs(row["id"])
        assert [run["status"] for run in runs] == ["succeeded", "failed"]
        assert runs[0]["final_text"] == "New"

    asyncio.run(finish())


def test_completion_after_deletion_does_not_recreate_task_or_emit_notifications(tmp_path, monkeypatch):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    row = db.create_scheduled_task({
        "id": "deleted", "title": "Delete", "prompt": "Work",
        "schedule_kind": "interval", "interval_seconds": 60,
    })
    monkeypatch.setattr(scheduler_module, "get_db", lambda: db)
    events = []
    monkeypatch.setattr(scheduler_module, "publish_ui_event", lambda *args: events.append(args))

    async def finish():
        service = ScheduledTaskService(settings_factory=lambda: object())
        run = _prepared_fixture(db, service, row)
        db.delete_scheduled_task(row["id"])
        await service._finish_run(row, run_id=run["run_id"], conversation_id=run["conversation_id"],
                                  status="succeeded", final_text="Done", error="")

    asyncio.run(finish())
    assert db.get_scheduled_task(row["id"]) is None
    assert db.list_scheduled_task_runs(row["id"]) == []
    assert events == []


def test_finalization_is_atomic_across_connections_and_rolls_back_invalid_update(tmp_path):
    first = Database(tmp_path / "agent.db")
    first.init_db()
    first.create_scheduled_task({
        "id": "concurrent", "title": "Concurrent", "prompt": "Work",
        "schedule_kind": "interval", "interval_seconds": 60,
    })
    run_id = first.create_scheduled_task_run(
        task_id="concurrent", conversation_id="fixture", started_at="2026-01-01T00:00:00+00:00",
        lease_owner="fixture", lease_expires_at="2099-01-01T00:00:00+00:00",
    )
    second = Database(tmp_path / "agent.db")
    second.init_db()
    args = dict(
        task_id="concurrent", run_id=run_id, conversation_id="fixture", lease_owner="fixture",
        finished_at="2026-01-01T00:01:00+00:00", status="failed", final_text="", error="Fixture failure",
    )
    with pytest.raises(ValueError, match="forbidden column"):
        first.finalize_scheduled_task_run(**args, prepare_task_update=lambda row: {"prompt": "Incorrect"})
    assert first.list_scheduled_task_runs("concurrent")[0]["status"] == "running"
    assert first.get_scheduled_task("concurrent")["prompt"] == "Work"

    barrier = Barrier(2)

    def finalize(db):
        barrier.wait(timeout=5)
        return db.finalize_scheduled_task_run(**args, prepare_task_update=lambda row: {
            "consecutive_failures": row["consecutive_failures"] + 1,
            "last_run_status": "error",
        })

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finalize, [first, second]))
    assert sum(result is not None for result in results) == 1
    assert first.get_scheduled_task("concurrent")["consecutive_failures"] == 1
    assert first.list_scheduled_task_runs("concurrent")[0]["status"] == "failed"
    first.close()
    second.close()


def test_configuration_revisions_reject_stale_edits_and_survive_reinitialization(tmp_path):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    row = db.create_scheduled_task({
        "id": "revision", "title": "Original", "prompt": "Work",
        "schedule_kind": "interval", "interval_seconds": 60,
    })
    updated = db.update_scheduled_task(row["id"], expected_config_revision=row["config_revision"], title="New")
    assert updated["config_revision"] == row["config_revision"] + 1
    with pytest.raises(ScheduledTaskConflictError):
        db.update_scheduled_task(row["id"], expected_config_revision=row["config_revision"], enabled=0)
    db.init_db()
    assert db.get_scheduled_task(row["id"]) == updated


def test_scheduler_renews_live_leases_while_waiting_for_runner_output(tmp_path, monkeypatch):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    row = db.create_scheduled_task({
        "id": "lease", "title": "Lease", "prompt": "Wait",
        "schedule_kind": "interval", "interval_seconds": 60,
    })
    monkeypatch.setattr(scheduler_module, "get_db", lambda: db)
    monkeypatch.setattr(scheduler_module, "_SCHEDULER_LEASE_RENEW_SECONDS", 0)
    renewed = asyncio.Event()
    original = db.renew_scheduled_task_run_lease

    def renew(*args, **kwargs):
        result = original(*args, **kwargs)
        if not kwargs.get("start"):
            assert result is True
            renewed.set()
        return result

    monkeypatch.setattr(db, "renew_scheduled_task_run_lease", renew)

    async def runner(**kwargs):
        await renewed.wait()
        yield {"event": "done", "data": {"summary": "Done"}}

    async def run():
        service = ScheduledTaskService(settings_factory=lambda: object(), runner=runner)
        await service.run_now(row["id"])
        await asyncio.wait_for(service._inflight[row["id"]][0], timeout=2)
        assert not any(task.get_name().startswith("scheduled-task-lease-") for task in asyncio.all_tasks())

    asyncio.run(run())
    assert renewed.is_set()
    assert db.list_scheduled_task_runs(row["id"])[0]["status"] == "succeeded"


def test_expired_or_recovered_lease_cannot_restart_or_finalize_an_attempt(tmp_path):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    db.create_scheduled_task({
        "id": "expired", "title": "Expired", "prompt": "Wait",
        "schedule_kind": "interval", "interval_seconds": 60,
    })
    run_id = db.create_scheduled_task_run(
        task_id="expired", conversation_id="fixture", started_at="2026-01-01T00:00:00+00:00",
        lease_owner="fixture", lease_expires_at="2026-01-01T00:01:00+00:00",
    )
    assert not db.renew_scheduled_task_run_lease(
        run_id, lease_owner="fixture", now_iso="2026-01-01T00:02:00+00:00",
        lease_expires_at="2026-01-01T00:20:00+00:00", start=True,
    )
    assert db.finalize_scheduled_task_run(
        task_id="expired", run_id=run_id, conversation_id="fixture", lease_owner="fixture",
        finished_at="2026-01-01T00:02:00+00:00", status="succeeded", final_text="Done", error="",
        prepare_task_update=lambda row: {"last_run_status": "ok"},
    ) is None
    db.recover_expired_scheduled_task_runs("2026-01-01T00:02:00+00:00")
    assert db.list_scheduled_task_runs("expired")[0]["status"] == "abandoned"
    assert not db.renew_scheduled_task_run_lease(
        run_id, lease_owner="fixture", now_iso="2026-01-01T00:00:30+00:00",
        lease_expires_at="2026-01-01T00:20:00+00:00", start=True,
    )


@pytest.mark.parametrize("change", ["lease_owner", "conversation_id", "task_id"])
def test_finalization_rejects_mismatched_attempt_identity(tmp_path, change):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    db.create_scheduled_task({
        "id": "identity", "title": "Identity", "prompt": "Work",
        "schedule_kind": "interval", "interval_seconds": 60,
    })
    run_id = db.create_scheduled_task_run(
        task_id="identity", conversation_id="fixture", started_at="2026-01-01T00:00:00+00:00",
        lease_owner="fixture", lease_expires_at="2099-01-01T00:00:00+00:00",
    )
    args = dict(task_id="identity", run_id=run_id, conversation_id="fixture", lease_owner="fixture",
                finished_at="2026-01-01T00:01:00+00:00", status="succeeded", final_text="Done", error="")
    args[change] = "another"

    def unexpected(row):
        raise AssertionError("Invalid attempt reached task-state calculation")

    assert db.finalize_scheduled_task_run(**args, prepare_task_update=unexpected) is None
    assert db.list_scheduled_task_runs("identity")[0]["status"] == "running"


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


def test_scheduled_task_create_tool_requires_approval(tmp_path, monkeypatch):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    monkeypatch.setattr(scheduling_tools, "get_db", lambda: db)
    created: list[dict] = []
    monkeypatch.setattr(
        scheduling_tools,
        "create_ticket",
        lambda **kwargs: created.append(kwargs) or type("Ticket", (), {"id": "ticket-1"})(),
    )

    payload = json.loads(
        scheduling_tools.scheduled_task_create(
            title="Exfil",
            prompt="Read documents",
            schedule_kind="interval",
            interval_seconds=60,
        )
    )

    assert payload["status"] == "pending_approval"
    assert created and created[0]["tool_name"] == "scheduled_task_create"
    assert db.list_scheduled_tasks() == []


def test_scheduled_task_tools_require_interactive_session(monkeypatch):
    token = run_context.set_current_interactive(False)
    try:
        create = json.loads(scheduling_tools.scheduled_task_create(title="t", prompt="p"))
        delete = json.loads(scheduling_tools.scheduled_task_delete("task-1"))
    finally:
        run_context.reset_current_interactive(token)

    assert create["reason_code"] == "interactive_session_required"
    assert delete["reason_code"] == "interactive_session_required"


def test_scheduled_task_rejects_unlisted_telegram_chat(monkeypatch):
    monkeypatch.setattr(scheduling_tools, "telegram_chat_allowed", lambda chat_id: chat_id == "111")

    payload = json.loads(
        scheduling_tools.scheduled_task_create(
            title="t", prompt="p", notify_telegram=True, telegram_chat_id="999"
        )
    )

    assert payload["reason_code"] == "telegram_chat_not_allowed"
