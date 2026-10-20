import asyncio
import os
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone

import pytest

from app.agent import scheduler
from app.agent.database import Database
from app.agent.database import ScheduledTaskCapacityError
from app.agent.harness.workers import mark_stopping


@pytest.fixture
def dispatch(tmp_path, monkeypatch):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    clock = [datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(scheduler, "get_db", lambda: db)
    monkeypatch.setattr(scheduler, "_utc_now", lambda: clock[0])
    yield db, clock
    db.close()


def make_task(db, clock, policy="skip", task_id="task"):
    return db.create_scheduled_task({
        "id": task_id, "title": task_id, "prompt": "Original prompt",
        "schedule_kind": "interval", "interval_seconds": 60, "overlap_policy": policy,
        "next_run_at": scheduler._iso_utc(clock[0]),
    })


@pytest.mark.parametrize("policy", ["skip", "queue", "cancel_previous"])
def test_normal_dispatch_reaches_each_overlap_policy_and_preserves_cadence(dispatch, policy):
    db, clock = dispatch
    make_task(db, clock, policy)

    async def run():
        started, second_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        async def runner(**kwargs):
            calls.append(kwargs["message"])
            if len(calls) == 1:
                started.set()
                await release.wait()
            else:
                second_started.set()
            yield {"event": "done", "data": {"summary": "Done"}}

        service = scheduler.ScheduledTaskService(settings_factory=lambda: object(), runner=runner)
        try:
            await service._fire_due_tasks()
            await asyncio.wait_for(started.wait(), 2)
            clock[0] += timedelta(seconds=60)
            await service._fire_due_tasks()
            assert db.get_scheduled_task("task")["next_run_at"] == scheduler._iso_utc(clock[0] + timedelta(seconds=60))
            if policy == "skip":
                assert len(calls) == 1
                assert db.list_scheduled_task_runs("task")[0]["status"] == "skipped"
            elif policy == "queue":
                assert len(calls) == 1
                assert db.list_scheduled_task_runs("task")[0]["status"] == "queued"
                db.update_scheduled_task("task", prompt="Changed after acceptance")
            else:
                await asyncio.wait_for(second_started.wait(), 2)
                assert db.list_scheduled_task_runs("task")[-1]["status"] == "cancelled"
            release.set()
            if policy == "queue":
                await asyncio.wait_for(second_started.wait(), 2)
                assert calls == ["Original prompt", "Original prompt"]
        finally:
            release.set()
            await service.stop()

    asyncio.run(run())


def test_queue_is_bounded_and_disable_cancels_pending_without_cancelling_active(dispatch):
    db, clock = dispatch
    make_task(db, clock, "queue")

    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def runner(**kwargs):
            calls.append(kwargs)
            started.set()
            await release.wait()
            yield {"event": "done", "data": {"summary": "Done"}}

        service = scheduler.ScheduledTaskService(settings_factory=lambda: object(), runner=runner)
        try:
            await service._fire_due_tasks()
            await asyncio.wait_for(started.wait(), 2)
            for _ in range(3):
                clock[0] += timedelta(seconds=60)
                await service._fire_due_tasks()
            runs = db.list_scheduled_task_runs("task")
            assert [row["status"] for row in runs].count("queued") == 1
            assert [row["status"] for row in runs].count("skipped") == 2
            db.update_scheduled_task("task", enabled=0, next_run_at="")
            await service.refresh("task")
            assert len(calls) == 1
            assert not service._inflight["task"][0].cancelled()
            assert not any(row["status"] == "queued" for row in db.list_scheduled_task_runs("task"))
        finally:
            release.set()
            await service.stop()

    asyncio.run(run())


def test_manual_retries_return_original_run_and_do_not_cancel_it(dispatch):
    db, clock = dispatch
    make_task(db, clock)

    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def runner(**kwargs):
            calls.append(kwargs)
            started.set()
            await release.wait()
            yield {"event": "done", "data": {"summary": "Done"}}

        service = scheduler.ScheduledTaskService(settings_factory=lambda: object(), runner=runner)
        try:
            first = await service.run_now("task", operation_key="same-request", requester="desktop")
            await asyncio.wait_for(started.wait(), 2)
            second = await service.run_now("task", operation_key="same-request", requester="desktop")
            assert second == first
            assert len(db.list_scheduled_task_runs("task")) == len(calls) == 1
            release.set()
            await service.stop()
            # A completed result is reconciled without creating another attempt.
            assert await service.run_now("task", operation_key="same-request", requester="desktop") == first
        finally:
            release.set()
            await service.stop()

    asyncio.run(run())


def test_replacement_waits_for_unfinished_native_worker(dispatch):
    db, clock = dispatch
    make_task(db, clock)
    worker = Future()

    async def run():
        started, replacement = asyncio.Event(), asyncio.Event()
        calls = []

        async def runner(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                mark_stopping(worker, kwargs["conversation_id"], frozenset({"fixture"}))
                started.set()
                await asyncio.Event().wait()
            replacement.set()
            yield {"event": "done", "data": {"summary": "Done"}}

        service = scheduler.ScheduledTaskService(settings_factory=lambda: object(), runner=runner)
        try:
            await service.run_now("task")
            await asyncio.wait_for(started.wait(), 2)
            await service.run_now("task")
            await asyncio.gather(*service._inflight["task"], return_exceptions=True)
            assert len(calls) == 1
            assert not replacement.is_set()
            worker.set_result("stopped")
            await service._fire_due_tasks()
            await asyncio.wait_for(replacement.wait(), 2)
        finally:
            if not worker.done():
                worker.set_result("stopped")
            await service.stop()

    asyncio.run(run())


def test_cadence_coalesces_offline_occurrences_and_completion_does_not_move_it(dispatch):
    db, clock = dispatch
    make_task(db, clock)
    clock[0] += timedelta(hours=4, seconds=35)

    async def runner(**kwargs):
        clock[0] += timedelta(seconds=10)
        yield {"event": "done", "data": {"summary": "Done"}}

    async def run():
        service = scheduler.ScheduledTaskService(settings_factory=lambda: object(), runner=runner)
        await service._fire_due_tasks()
        await asyncio.gather(*service._inflight["task"])
        await service.stop()

    asyncio.run(run())
    runs = db.list_scheduled_task_runs("task")
    assert len(runs) == 1
    assert runs[0]["scheduled_for"] == "2026-10-20T04:00:00+00:00"
    assert runs[0]["coalesced"] == 1
    assert db.get_scheduled_task("task")["next_run_at"] == "2026-10-20T04:01:00+00:00"


@pytest.mark.parametrize("after,expression,expected", [
    ("2026-03-08T06:00:00+00:00", "30 2 * * *", "2026-03-09T06:30:00+00:00"),
    ("2026-11-01T05:00:00+00:00", "30 1 * * *", "2026-11-01T05:30:00+00:00"),
    ("2026-11-01T05:31:00+00:00", "30 1 * * *", "2026-11-02T06:30:00+00:00"),
    ("2026-11-01T06:15:00+00:00", "30 1 * * *", "2026-11-02T06:30:00+00:00"),
])
def test_cron_skips_nonexistent_times_and_uses_first_ambiguous_occurrence(after, expression, expected):
    row = {"schedule_kind": "cron", "cron_expr": expression, "timezone": "America/New_York"}
    assert scheduler._iso_utc(scheduler.ScheduledTaskService.compute_next_run(row, after=datetime.fromisoformat(after))) == expected


def test_expired_accepted_queue_is_recovered_once_without_replaying_running_work(dispatch):
    db, clock = dispatch
    make_task(db, clock)
    first = scheduler.ScheduledTaskService(settings_factory=lambda: object())
    accepted = first._accept_occurrence("task", expected_due=scheduler._iso_utc(clock[0]))
    assert accepted["status"] == "queued"
    assert len(db.list_conversations()) == 1
    clock[0] += timedelta(minutes=16)

    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def runner(**kwargs):
            calls.append(kwargs)
            started.set()
            await release.wait()
            yield {"event": "done", "data": {"summary": "Done"}}

        second = scheduler.ScheduledTaskService(settings_factory=lambda: object(), runner=runner)
        try:
            await second._fire_due_tasks()
            await asyncio.wait_for(started.wait(), 2)
            assert len(calls) == 1
            assert db.list_scheduled_task_runs("task")[-1]["id"] == accepted["id"]
            await second._fire_due_tasks()
            assert len(calls) == 1
        finally:
            release.set()
            await second.stop()

    asyncio.run(run())


def test_atomic_acceptance_competing_connections_and_failed_build_leave_no_orphans(dispatch):
    db, clock = dispatch
    row = make_task(db, clock)
    first = scheduler.ScheduledTaskService(settings_factory=lambda: object())

    def failed_build(current):
        raise RuntimeError("fixture build failed")

    with pytest.raises(RuntimeError):
        db.accept_scheduled_occurrence(
            row["id"], now_iso=scheduler._iso_utc(clock[0]), lease_owner="fixture",
            lease_expires_at="2099-01-01T00:00:00+00:00", expected_due=row["next_run_at"], build=failed_build,
        )
    assert db.list_scheduled_task_runs(row["id"]) == []
    assert db.list_conversations() == []
    assert db.get_scheduled_task(row["id"])["next_run_at"] == row["next_run_at"]
    accepted = first._accept_occurrence(row["id"], expected_due=row["next_run_at"])
    second = Database(db._path)
    second.init_db()
    try:
        assert second.accept_scheduled_occurrence(
            row["id"], now_iso=scheduler._iso_utc(clock[0]), lease_owner="other",
            lease_expires_at="2099-01-01T00:00:00+00:00", expected_due=row["next_run_at"], build=failed_build,
        ) is None
        assert db.start_scheduled_occurrence(accepted["id"], first._lease_owner, scheduler._iso_utc(clock[0]), "2099-01-01T00:00:00+00:00", 4)
        assert not second.start_scheduled_occurrence(accepted["id"], "other", scheduler._iso_utc(clock[0]), "2099-01-01T00:00:00+00:00", 4)
        assert len(db.list_scheduled_task_runs("task")) == len(db.list_conversations()) == 1
    finally:
        second.close()


def test_full_global_queue_rejects_manual_request_before_cancelling_existing_work(dispatch):
    db, clock = dispatch
    for task_id in ("first", "second", "third"):
        make_task(db, clock, "queue", task_id)

    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def runner(**kwargs):
            started.set()
            await release.wait()
            yield {"event": "done", "data": {"summary": "Done"}}

        service = scheduler.ScheduledTaskService(settings_factory=lambda: object(), runner=runner,
                                                 max_global_concurrency=1, max_queued_occurrences=1)
        try:
            await service.run_now("first")
            await asyncio.wait_for(started.wait(), 2)
            await service.run_now("second")
            with pytest.raises(ScheduledTaskCapacityError):
                await service.run_now("first")
            assert db.list_scheduled_task_runs("first")[0]["cancel_requested"] == 0
            assert len(db.list_scheduled_task_runs("first")) == 1
        finally:
            release.set()
            await service.stop()

    asyncio.run(run())


def test_api_lost_response_reconciliation_and_caller_scoping(dispatch, monkeypatch):
    import httpx
    from app.main import app
    from app.api.routes import scheduled_tasks as routes
    from app.security.control_plane import mint_control_token

    db, clock = dispatch
    make_task(db, clock)
    monkeypatch.setattr(routes, "get_db", lambda: db)

    async def run():
        async def runner(**kwargs):
            yield {"event": "done", "data": {"summary": "Done"}}

        service = scheduler.ScheduledTaskService(settings_factory=lambda: object(), runner=runner)
        monkeypatch.setattr(routes, "get_scheduled_task_service", lambda: service)
        token = mint_control_token(os.environ["MONAW_CONTROL_SECRET"], session_id="caller-one")
        other = mint_control_token(os.environ["MONAW_CONTROL_SECRET"], session_id="caller-two")
        headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "lost-response"}
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
                first = await client.post("/api/scheduled-tasks/task/run", headers=headers)
                assert first.status_code == 200
                recovered = await client.get("/api/scheduled-tasks/task/operations/lost-response", headers=headers)
                assert recovered.json() == first.json()
                retry = await client.post("/api/scheduled-tasks/task/run", headers=headers)
                assert retry.json() == first.json()
                hidden = await client.get("/api/scheduled-tasks/task/operations/lost-response", headers={"Authorization": f"Bearer {other}"})
                assert hidden.status_code == 404
                assert len(db.list_scheduled_task_runs("task")) == 1
        finally:
            await service.stop()

    asyncio.run(run())


def test_replacement_before_first_loop_turn_does_not_strand_reservation(dispatch):
    db, clock = dispatch
    make_task(db, clock)

    async def run():
        started = asyncio.Event()
        calls = []

        async def runner(**kwargs):
            calls.append(kwargs)
            started.set()
            yield {"event": "done", "data": {"summary": "Done"}}

        service = scheduler.ScheduledTaskService(settings_factory=lambda: object(), runner=runner)
        try:
            first = await service.run_now("task")
            second = await service.run_now("task")
            await asyncio.wait_for(started.wait(), 2)
            assert len(calls) == 1
            assert calls[0]["conversation_id"] == second["conversation_id"]
            assert next(row for row in db.list_scheduled_task_runs("task") if row["id"] == first["run_id"])["status"] == "cancelled"
        finally:
            await service.stop()

    asyncio.run(run())


def test_operation_identity_survives_run_history_retention(dispatch):
    db, clock = dispatch
    make_task(db, clock)

    async def runner(**kwargs):
        yield {"event": "done", "data": {"summary": "Done"}}

    async def run():
        service = scheduler.ScheduledTaskService(settings_factory=lambda: object(), runner=runner)
        accepted = await service.run_now("task", operation_key="retained", requester="desktop")
        await service.stop()
        db.execute("UPDATE scheduled_task_runs SET finished_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (accepted["run_id"],))
        db.commit()
        assert db.enforce_scheduled_task_run_retention()["scheduled_task_runs_deleted"] == 1
        assert await service.run_now("task", operation_key="retained", requester="desktop") == accepted
        assert db.list_scheduled_task_runs("task") == []

    asyncio.run(run())


def test_legacy_cleared_claim_is_repaired_without_replaying_abandoned_attempt(dispatch):
    db, clock = dispatch
    row = make_task(db, clock)
    db.update_scheduled_task(row["id"], next_run_at="")
    db.repair_scheduled_intent(scheduler._iso_utc(clock[0]), lambda current: scheduler.ScheduledTaskService._next_iso(current, clock[0]))
    assert db.get_scheduled_task(row["id"])["next_run_at"] == scheduler._iso_utc(clock[0] + timedelta(seconds=60))
    assert db.list_scheduled_task_runs(row["id"]) == []


def test_upgrade_from_revision_only_schema_preserves_existing_tasks_and_results(dispatch):
    db, clock = dispatch
    task = make_task(db, clock)
    run_id = db.create_scheduled_task_run(task_id=task["id"], conversation_id="legacy-result",
                                         started_at="2026-01-01T00:00:00+00:00", status="succeeded")
    db.update_scheduled_task_run(run_id, finished_at="2026-01-01T00:01:00+00:00", final_text="Keep this result")
    # Reconstruct the additive migration's actual predecessor schema, retaining
    # rows and the earlier migrations, rather than testing only an empty DB.
    db.execute("DROP TABLE scheduled_run_requests")
    db.execute("DROP INDEX idx_scheduled_pending")
    for column in ("task_snapshot", "manual", "scheduled_for", "coalesced", "cancel_requested"):
        db.execute(f"ALTER TABLE scheduled_task_runs DROP COLUMN {column}")
    db.execute("DELETE FROM schema_migrations WHERE version = 16")
    db.commit()
    db.init_db()
    db.init_db()
    assert db.get_scheduled_task(task["id"])["prompt"] == "Original prompt"
    result = db.list_scheduled_task_runs(task["id"])[0]
    assert result["id"] == run_id
    assert result["status"] == "succeeded"
    assert result["final_text"] == "Keep this result"
    assert result["task_snapshot"] == ""


def test_queued_occurrence_does_not_hide_active_failure_threshold(dispatch):
    db, clock = dispatch
    make_task(db, clock, "queue")
    db.update_scheduled_task("task", consecutive_failures=4)

    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def runner(**kwargs):
            calls.append(kwargs)
            started.set()
            await release.wait()
            yield {"event": "error", "data": {"message": "Fifth failure"}}

        service = scheduler.ScheduledTaskService(settings_factory=lambda: object(), runner=runner)
        try:
            await service._fire_due_tasks()
            await asyncio.wait_for(started.wait(), 2)
            clock[0] += timedelta(seconds=60)
            await service._fire_due_tasks()
            assert db.list_scheduled_task_runs("task")[0]["status"] == "queued"
            release.set()
            await asyncio.gather(*service._inflight["task"])
            assert len(calls) == 1
            assert db.get_scheduled_task("task")["enabled"] == 0
            assert db.get_scheduled_task("task")["consecutive_failures"] == 5
            assert [row["status"] for row in db.list_scheduled_task_runs("task")] == ["cancelled", "dead_letter"]
        finally:
            release.set()
            await service.stop()

    asyncio.run(run())
