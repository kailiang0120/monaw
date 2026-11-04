"""HTTP acceptance tests for durable Run ownership, controls, and event replay."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.agent.approval_broker import TicketStatus, create_ticket, get_ticket
from app.agent.database import get_db
from app.agent.durable.journal import Journal
from app.agent.durable.turn_run import create_durable_run
from app.agent.file_mutation.fingerprint import fingerprint_path
from app.agent.file_mutation.planner import PathState, PlanOperand, build_plan
from app.agent.file_mutation.snapshot_store import SnapshotStore
from app.agent.file_mutation.verifier import FileMutationVerifier
from app.agent.run_context import (
    reset_current_control_session_id,
    reset_current_conversation_id,
    reset_current_execution_source,
    set_current_control_session_id,
    set_current_conversation_id,
    set_current_execution_source,
)
from app.agent.execution_gate import ExecutionGateService
from app.agent.iteration_budget import IterationBudget
from app.main import app
from app.security.control_plane import ALL_SCOPES, mint_control_token


PROFILE_ID = "profile-desktop-a"
OLD_SESSION_ID = "session-before-restart"
NEW_SESSION_ID = "session-after-restart"


@pytest.fixture
def runs_api_context(tmp_path: Path):
    db = get_db()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    conversation_id = "chat-runs-api"
    db.create_conversation(conversation_id, "Runs API", str(workspace))
    run = create_durable_run(
        conversation_id=conversation_id,
        execution_source="desktop",
        principal_id=PROFILE_ID,
        control_session_id=OLD_SESSION_ID,
        workspace_dir=str(workspace),
        run_id="run-api-desktop",
        turn_id="turn-api-desktop",
        db=db,
    )
    yield {
        "client": TestClient(app),
        "db": db,
        "journal": run.journal,
        "workspace": workspace,
        "conversation_id": conversation_id,
        "run": run,
    }


def _token(*, session_id: str, profile_id: str | None = PROFILE_ID, scopes=ALL_SCOPES) -> str:
    import os

    return mint_control_token(
        os.environ["MONAW_CONTROL_SECRET"],
        scopes=scopes,
        session_id=session_id,
        profile_id=profile_id,
    )


def _headers(
    *,
    session_id: str = NEW_SESSION_ID,
    profile_id: str | None = PROFILE_ID,
    idempotency_key: str | None = None,
) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {_token(session_id=session_id, profile_id=profile_id)}"}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _create_run(
    context,
    *,
    run_id: str,
    conversation_id: str,
    workspace: Path,
    execution_source: str = "desktop",
    principal_id: str = PROFILE_ID,
    control_session_id: str = OLD_SESSION_ID,
):
    context["db"].create_conversation(conversation_id, run_id, str(workspace))
    return create_durable_run(
        conversation_id=conversation_id,
        execution_source=execution_source,
        principal_id=principal_id,
        control_session_id=control_session_id,
        workspace_dir=str(workspace),
        run_id=run_id,
        turn_id=f"turn-{run_id}",
        db=context["db"],
    )


def _pause_for_resume(run) -> dict:
    db = run.journal.db
    db.persist_turn_messages(
        run.turn_id,
        "chat-runs-api" if run.run_id == "run-api-desktop" else run.journal.snapshot(run.run_id)["conversation_id"],
        [{"role": "user", "content": "Continue this task safely."}],
    )
    latest = run.journal.snapshot(run.run_id)
    pausing = run.journal.transition_run(
        run.run_id,
        to_status="pausing",
        owner_instance_id=run.owner_instance_id,
        attempt=run.attempt,
        expected_revision=int(latest["revision"]),
        reason_code="pause_requested",
    )
    return run.journal.transition_run(
        run.run_id,
        to_status="paused",
        owner_instance_id=run.owner_instance_id,
        attempt=run.attempt,
        expected_revision=int(pausing["revision"]),
        reason_code="user_pause",
        recovery_input_ref=f"turn_checkpoint:{run.turn_id}",
    )


def _prepare_verifiable_recovery(context, tmp_path: Path, *, operation_id: str = "resolve-api-operation"):
    """Create a real private P3 snapshot and an interrupted Journal operation."""
    run = context["run"]
    workspace = context["workspace"]
    target = workspace / f"{operation_id}.txt"
    before_bytes = b"before-image from P3\n"
    after_bytes = b"verified after-image from P3\n"
    target.write_bytes(before_bytes)

    fingerprint = fingerprint_path(target, include_bytes=True)
    before = PathState.from_fingerprint(fingerprint)
    after = PathState.from_bytes(
        target,
        after_bytes,
        identity=before.identity,
        parent_identity=before.parent_identity,
    )
    plan = build_plan(
        "write",
        {"path": str(target), "content": after_bytes.decode("utf-8")},
        [PlanOperand(str(target), "target", before, after, identity_strategy="same")],
        before_bytes=before_bytes,
        after_bytes=after_bytes,
        read_authorized=True,
    )
    store = SnapshotStore(tmp_path / f"{operation_id}-snapshots")
    snapshot_ref = store.capture(
        plan,
        {str(target): before_bytes},
        run_id=run.run_id,
        operation_id=operation_id,
        authorized_roots=[str(workspace)],
    )

    journal = context["journal"]
    operation = journal.prepare_operation(
        run.run_id,
        operation_id=operation_id,
        owner_instance_id=run.owner_instance_id,
        attempt=run.attempt,
        operation_kind="file_write",
        effect_class="opaque_effect",
        tool_name="write_file",
    )
    operation = journal.transition_operation(
        run.run_id,
        operation_id,
        to_status="authorized",
        owner_instance_id=run.owner_instance_id,
        attempt=run.attempt,
    )
    operation = journal.transition_operation(
        run.run_id,
        operation_id,
        to_status="executing",
        owner_instance_id=run.owner_instance_id,
        attempt=run.attempt,
    )
    journal.promote_operation_to_verifiable_write(
        run.run_id,
        operation_id,
        owner_instance_id=run.owner_instance_id,
        attempt=run.attempt,
        snapshot_ref=snapshot_ref,
        input_summary={"snapshot_available": True},
        precondition_evidence={"verification_result": "not_checked"},
    )
    journal.transition_operation(
        run.run_id,
        operation_id,
        to_status="uncertain",
        owner_instance_id=run.owner_instance_id,
        attempt=run.attempt,
        reason_code="worker_lost",
    )
    journal.transition_operation(
        run.run_id,
        operation_id,
        to_status="needs_review",
        owner_instance_id=run.owner_instance_id,
        attempt=run.attempt,
        reason_code="external_effect_unknown",
    )
    context["db"].persist_turn_messages(
        run.turn_id,
        context["conversation_id"],
        [{"role": "user", "content": "Safely verify the previous file operation."}],
    )
    current = journal.snapshot(run.run_id)
    journal.transition_run(
        run.run_id,
        to_status="needs_review",
        owner_instance_id=run.owner_instance_id,
        attempt=run.attempt,
        expected_revision=int(current["revision"]),
        reason_code="external_effect_unknown",
    )
    return {"operation_id": operation_id, "target": target, "before": before_bytes, "after": after_bytes, "store": store}


def _resolve_body(context, *, resolution: str = "verify", revision: int | None = None) -> dict:
    snapshot = context["journal"].snapshot(context["run"].run_id)
    return {
        "expected_revision": int(snapshot["revision"] if revision is None else revision),
        "expected_attempt": int(snapshot["attempt"]),
        "resolution": resolution,
    }


def _resolve_url(context, operation_id: str) -> str:
    return f"/api/runs/{context['run'].run_id}/operations/{operation_id}/resolve"


def _use_real_verifier(monkeypatch: pytest.MonkeyPatch, store: SnapshotStore, *, read_policy=None):
    import app.agent.file_mutation.verifier as verifier_module

    verifier = FileMutationVerifier(store, read_policy=read_policy or (lambda _path: True))
    monkeypatch.setattr(verifier_module, "_DEFAULT_VERIFIER", verifier)
    return verifier


def _prepare_opaque_recovery(context, *, operation_id: str = "opaque-resolve-operation"):
    run = context["run"]
    journal = context["journal"]
    operation = journal.prepare_operation(
        run.run_id,
        operation_id=operation_id,
        owner_instance_id=run.owner_instance_id,
        attempt=run.attempt,
        operation_kind="shell_command",
        effect_class="opaque_effect",
        tool_name="shell_command",
    )
    for status in ("authorized", "executing", "uncertain", "needs_review"):
        operation = journal.transition_operation(
            run.run_id,
            operation_id,
            to_status=status,
            owner_instance_id=run.owner_instance_id,
            attempt=run.attempt,
        )
    context["db"].persist_turn_messages(
        run.turn_id,
        context["conversation_id"],
        [{"role": "user", "content": "Inspect the interrupted task."}],
    )
    current = journal.snapshot(run.run_id)
    journal.transition_run(
        run.run_id,
        to_status="needs_review",
        owner_instance_id=run.owner_instance_id,
        attempt=run.attempt,
        expected_revision=int(current["revision"]),
        reason_code="external_effect_unknown",
    )
    return operation


def test_same_profile_can_read_a_run_after_control_session_restart(runs_api_context):
    context = runs_api_context

    response = context["client"].get(
        f"/api/runs/{context['run'].run_id}",
        headers=_headers(session_id=NEW_SESSION_ID, profile_id=PROFILE_ID),
    )

    assert response.status_code == 200, response.text
    assert response.json()["run_id"] == context["run"].run_id
    assert response.json()["conversation_id"] == context["conversation_id"]


def test_missing_profile_is_rejected_before_run_lookup(runs_api_context, monkeypatch: pytest.MonkeyPatch):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("Run storage must not be read before profile authorization")

    monkeypatch.setattr(Journal, "snapshot", unexpected)

    response = runs_api_context["client"].get(
        "/api/runs/not-a-real-run",
        headers=_headers(session_id=NEW_SESSION_ID, profile_id=None),
    )

    assert response.status_code == 403


def test_other_profile_background_run_and_changed_workspace_are_isolated(runs_api_context):
    context = runs_api_context
    other_workspace = context["workspace"].parent / "other-workspace"
    other_workspace.mkdir()
    other_run = _create_run(
        context,
        run_id="run-api-other-profile",
        conversation_id="chat-api-other-profile",
        workspace=other_workspace,
        principal_id="profile-desktop-b",
        control_session_id="other-profile-session",
    )
    telegram_run = _create_run(
        context,
        run_id="run-api-telegram",
        conversation_id="chat-api-telegram",
        workspace=other_workspace,
        execution_source="telegram",
        principal_id=PROFILE_ID,
        control_session_id="telegram-owner",
    )
    scheduled_run = _create_run(
        context,
        run_id="run-api-scheduled",
        conversation_id="chat-api-scheduled",
        workspace=other_workspace,
        execution_source="scheduled",
        principal_id=PROFILE_ID,
        control_session_id="scheduler-owner",
    )

    for hidden_run in (other_run, telegram_run, scheduled_run):
        hidden = context["client"].get(
            f"/api/runs/{hidden_run.run_id}", headers=_headers()
        )
        assert hidden.status_code == 404, hidden.text

    listing = context["client"].get(
        "/api/runs?conversation_id=chat-api-other-profile", headers=_headers()
    )
    assert listing.status_code == 200, listing.text
    assert all(row["run_id"] != other_run.run_id for row in listing.json()["runs"])

    changed_workspace = context["workspace"].parent / "replacement-workspace"
    changed_workspace.mkdir()
    context["db"].update_conversation(context["conversation_id"], workspace_dir=str(changed_workspace))
    stale = context["client"].get(
        f"/api/runs/{context['run'].run_id}", headers=_headers()
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "workspace_changed"


def test_resume_is_revision_fenced_and_works_for_same_profile_after_restart(
    runs_api_context, monkeypatch: pytest.MonkeyPatch
):
    context = runs_api_context
    paused = _pause_for_resume(context["run"])
    from app.api.routes import runs

    started: list[tuple[str, str]] = []

    def fake_start_resume_worker(handle, _resume_input):
        started.append((handle.run_id, handle.turn_id))

    monkeypatch.setattr(runs, "_start_resume_worker", fake_start_resume_worker)
    body = {"expected_revision": int(paused["revision"]), "expected_attempt": int(paused["attempt"])}
    headers = _headers(idempotency_key="resume-same-profile-after-restart")

    stale = context["client"].post(
        f"/api/runs/{context['run'].run_id}/resume",
        json={**body, "expected_revision": int(paused["revision"]) - 1},
        headers=_headers(idempotency_key="resume-stale-revision"),
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "revision_conflict"
    assert context["journal"].snapshot(context["run"].run_id)["status"] == "paused"

    resumed = context["client"].post(
        f"/api/runs/{context['run'].run_id}/resume", json=body, headers=headers
    )
    assert resumed.status_code == 202, resumed.text
    result = resumed.json()
    assert result["accepted"] is True
    assert result["run"]["status"] == "running"
    assert result["run"]["attempt"] == int(paused["attempt"]) + 1

    idem = context["db"].fetchone(
        "SELECT state, idempotency_key_hash FROM run_command_idempotency WHERE run_id = ?",
        (context["run"].run_id,),
    )
    assert idem is not None and idem["state"] == "completed"
    assert idem["idempotency_key_hash"] != "resume-same-profile-after-restart"

    # A new HTTP client/session can recover the saved response after process restart.
    retry = context["client"].post(
        f"/api/runs/{context['run'].run_id}/resume",
        json=body,
        headers=_headers(session_id="session-after-second-restart", idempotency_key="resume-same-profile-after-restart"),
    )
    assert retry.status_code == 202, retry.text
    assert retry.json() == result
    assert len(started) == 1
    assert started[0][0] == context["run"].run_id


def test_resume_dispatches_a_real_worker_and_finalizes_the_new_attempt(
    runs_api_context, monkeypatch: pytest.MonkeyPatch
):
    context = runs_api_context
    paused = _pause_for_resume(context["run"])
    from app.agent import runtime, settings_store
    from app.agent.steering import get_steering
    from app.api.routes import runs

    monkeypatch.setattr(settings_store, "build_runtime_namespace", lambda *_args: object())
    monkeypatch.setattr(settings_store, "load_agent_settings", lambda *_args: object())
    observed: list[dict[str, object]] = []

    async def fake_run_agent_stream(
        *,
        message,
        conversation_id,
        control_session_id,
        execution_source,
        principal_id,
        permission_profile_id,
        interactive,
        steering,
        durable_run,
        attachments,
        settings,
    ):
        observed.append(
            {
                "message": message,
                "conversation_id": conversation_id,
                "control_session_id": control_session_id,
                "execution_source": execution_source,
                "principal_id": principal_id,
                "permission_profile_id": permission_profile_id,
                "interactive": interactive,
                "is_continuation": durable_run.is_continuation,
                "attempt": durable_run.attempt,
                "attachments": attachments,
                "steering": steering,
                "settings": settings,
            }
        )
        context["db"].persist_turn_messages(
            durable_run.turn_id,
            conversation_id,
            [{"role": "user", "content": "Continued by the test worker."}],
        )
        assert durable_run.finalize("complete")
        yield {
            "event": "done",
            "data": {"conversation_id": conversation_id, "summary": "continued", "status": "complete"},
        }

    monkeypatch.setattr(runtime, "run_agent_stream", fake_run_agent_stream)
    ui_events: list[tuple[str, dict]] = []
    monkeypatch.setattr(runs, "publish_ui_event", lambda name, data: ui_events.append((name, data)))
    request = SimpleNamespace(
        state=SimpleNamespace(
            control_session=SimpleNamespace(
                session_id=NEW_SESSION_ID,
                profile_id=PROFILE_ID,
                expires_at=4_000_000_000,
            )
        )
    )
    followed: list[bytes] = []

    async def scenario():
        response = await runs.resume_run_route(
            context["run"].run_id,
            runs.RunCommandIn(
                expected_revision=int(paused["revision"]),
                expected_attempt=int(paused["attempt"]),
            ),
            request,
            idempotency_key="resume-start-real-worker",
        )
        assert response["accepted"] is True
        inbox = get_steering(context["run"].run_id, NEW_SESSION_ID)
        assert inbox is not None and inbox.task is not None
        # The chat follows a resumed Run like any running turn, continuing the paused reply.
        assert inbox.continuation is True
        subscription = inbox.stream.subscribe()
        await asyncio.wait_for(inbox.task, timeout=3)
        while (chunk := await subscription.get()) is not None:
            followed.append(chunk)
        return response

    response = asyncio.run(scenario())

    assert followed and followed[0].startswith(b"event: turn_started")
    assert any(chunk.startswith(b"event: done") for chunk in followed)
    actions = [data.get("action") for name, data in ui_events if name == "conversation.changed"]
    assert actions[0] == "run_started" and actions[-1] == "run_finished" and "messages_changed" in actions
    assert response["run"]["status"] == "running"
    assert len(observed) == 1
    assert observed[0]["conversation_id"] == context["conversation_id"]
    assert observed[0]["control_session_id"] == NEW_SESSION_ID
    assert observed[0]["principal_id"] == PROFILE_ID
    assert observed[0]["execution_source"] == "desktop"
    assert observed[0]["is_continuation"] is True
    assert observed[0]["attempt"] == int(paused["attempt"]) + 1
    current = context["journal"].snapshot(context["run"].run_id)
    assert current["status"] == "completed"
    assert current["attempt"] == int(paused["attempt"]) + 1


def test_parallel_resume_requests_with_one_idempotency_key_start_one_attempt(
    runs_api_context, monkeypatch: pytest.MonkeyPatch
):
    context = runs_api_context
    paused = _pause_for_resume(context["run"])
    from app.api.routes import runs

    started: list[str] = []

    def start_once(handle, _resume_input):
        started.append(handle.run_id)

    monkeypatch.setattr(runs, "_start_resume_worker", start_once)
    complete_entered = threading.Event()
    complete_release = threading.Event()
    original_complete = runs._complete_idempotency

    def block_first_completion(**kwargs):
        complete_entered.set()
        assert complete_release.wait(timeout=5)
        original_complete(**kwargs)

    monkeypatch.setattr(runs, "_complete_idempotency", block_first_completion)
    body = {"expected_revision": int(paused["revision"]), "expected_attempt": int(paused["attempt"])}
    headers = _headers(idempotency_key="resume-parallel-key")

    barrier = threading.Barrier(3)

    def submit():
        barrier.wait(timeout=5)
        return context["client"].post(
            f"/api/runs/{context['run'].run_id}/resume", json=body, headers=headers
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(submit)
        second = pool.submit(submit)
        barrier.wait(timeout=5)
        assert complete_entered.wait(timeout=5)
        time.sleep(0.1)
        complete_release.set()
        responses = [first.result(timeout=10), second.result(timeout=10)]

    assert [response.status_code for response in responses] == [202, 202], [r.text for r in responses]
    assert responses[0].json() == responses[1].json()
    current = context["journal"].snapshot(context["run"].run_id)
    assert current["attempt"] == int(paused["attempt"]) + 1
    assert started == [context["run"].run_id]


def test_resume_idempotency_key_rejects_a_changed_request(runs_api_context, monkeypatch: pytest.MonkeyPatch):
    context = runs_api_context
    paused = _pause_for_resume(context["run"])
    from app.api.routes import runs

    monkeypatch.setattr(runs, "_start_resume_worker", lambda *_args: None)
    route = f"/api/runs/{context['run'].run_id}/resume"
    headers = _headers(idempotency_key="resume-payload-conflict")
    body = {"expected_revision": int(paused["revision"]), "expected_attempt": int(paused["attempt"])}
    first = context["client"].post(route, json=body, headers=headers)
    assert first.status_code == 202, first.text

    conflict = context["client"].post(
        route,
        json={**body, "expected_attempt": int(paused["attempt"]) + 1},
        headers=headers,
    )

    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"


def test_run_command_idempotency_migration_is_repeatable_and_has_database_fences(runs_api_context):
    from app.agent.migrations.runner import apply_migrations

    context = runs_api_context
    db = context["db"]
    with db.transaction() as conn:
        assert apply_migrations(conn) == []
        columns = conn.execute("PRAGMA table_info(run_command_idempotency)").fetchall()
        key_columns = [str(row["name"]) for row in sorted(columns, key=lambda row: int(row["pk"])) if int(row["pk"]) > 0]
        assert key_columns == [
            "principal_hash",
            "action",
            "run_id",
            "resource_id",
            "idempotency_key_hash",
        ]
        foreign_keys = conn.execute("PRAGMA foreign_key_list(run_command_idempotency)").fetchall()
        assert any(str(row["table"]) == "agent_runs" and str(row["on_delete"]).upper() == "CASCADE" for row in foreign_keys)

    row = (
        "a" * 64,
        "resolve",
        context["run"].run_id,
        "op-migration-check",
        "b" * 64,
        "c" * 64,
        "completed",
        "{}",
        None,
        None,
        "2026-11-04T00:00:00+00:00",
        "2026-11-04T00:00:00+00:00",
    )
    insert_sql = """INSERT INTO run_command_idempotency (
        principal_hash, action, run_id, resource_id, idempotency_key_hash, payload_hash,
        state, response_json, claim_owner, claim_lease_expires_at, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    with db.transaction() as conn:
        conn.execute(insert_sql, row)
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute(insert_sql, row)
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute(insert_sql, ("d" * 64, "not-a-run-action", *row[2:]))
    with db.transaction() as conn:
        conn.execute(insert_sql, (*row[:3], "another-operation", *row[4:]))


def test_resolve_completed_snapshot_with_real_p3_verifier_pauses_and_enables_resume(
    runs_api_context, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    context = runs_api_context
    evidence = _prepare_verifiable_recovery(context, tmp_path)
    evidence["target"].write_bytes(evidence["after"])
    verifier = _use_real_verifier(monkeypatch, evidence["store"])
    verification_calls = 0
    original_verify = verifier.verify

    def count_verify(operation):
        nonlocal verification_calls
        verification_calls += 1
        return original_verify(operation)

    monkeypatch.setattr(verifier, "verify", count_verify)
    body = _resolve_body(context)
    headers = _headers(idempotency_key="resolve-completed-key")
    url = _resolve_url(context, evidence["operation_id"])

    first = context["client"].post(url, json=body, headers=headers)
    assert first.status_code == 202, first.text
    response = first.json()
    assert response["verification_result"] == "completed"
    assert response["operation"]["status"] == "verified"
    assert response["operation"]["evidence"]["sha256_matches"] is True
    assert response["run"]["status"] == "paused"
    assert "resume" in response["run"]["available_actions"]

    # A new client session with the same signed profile gets the stored result;
    # it cannot verify again or consume a newly changed filesystem state.
    evidence["target"].write_bytes(b"changed after the first verification")
    replay = TestClient(app).post(
        url,
        json=body,
        headers=_headers(session_id="session-after-resolve-restart", idempotency_key="resolve-completed-key"),
    )
    assert replay.status_code == 202, replay.text
    assert replay.json() == response
    assert verification_calls == 1

    record = context["db"].fetchone(
        "SELECT action, resource_id, state FROM run_command_idempotency WHERE run_id = ? AND action = 'resolve'",
        (context["run"].run_id,),
    )
    assert record is not None
    assert record["resource_id"] == evidence["operation_id"]
    assert record["state"] == "completed"


def test_resolve_unchanged_snapshot_is_safe_to_retry_and_enables_resume(
    runs_api_context, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    context = runs_api_context
    evidence = _prepare_verifiable_recovery(context, tmp_path, operation_id="resolve-unchanged-operation")
    _use_real_verifier(monkeypatch, evidence["store"])

    response = context["client"].post(
        _resolve_url(context, evidence["operation_id"]),
        json=_resolve_body(context),
        headers=_headers(idempotency_key="resolve-unchanged-key"),
    )

    assert response.status_code == 202, response.text
    result = response.json()
    assert result["verification_result"] == "unchanged"
    assert result["operation"]["status"] == "failed"
    assert result["operation"]["reason_code"] == "safe_to_retry"
    assert result["operation"]["evidence"]["verification_result"] == "unchanged"
    assert result["run"]["status"] == "paused"
    assert "resume" in result["run"]["available_actions"]


def test_resolve_unknown_when_current_read_permission_is_revoked_stays_needs_review(
    runs_api_context, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.agent.conversation_workspace import current_workspace_dir
    from app.agent.run_context import (
        current_control_session_id,
        current_conversation_id,
        current_execution_source,
        current_interactive,
        current_permission_profile_id,
        current_principal_id,
    )

    context = runs_api_context
    evidence = _prepare_verifiable_recovery(context, tmp_path, operation_id="resolve-permission-operation")
    evidence["target"].write_bytes(evidence["after"])
    observed_context: list[tuple[str, ...]] = []

    def revoked_read(_path: str):
        observed_context.append(
            (
                str(current_workspace_dir()),
                current_conversation_id(),
                current_control_session_id(),
                current_principal_id(),
                current_permission_profile_id(),
                current_execution_source(),
                str(current_interactive()),
            )
        )
        return {"allowed": False, "requires_access_grant": True}

    _use_real_verifier(monkeypatch, evidence["store"], read_policy=revoked_read)
    response = context["client"].post(
        _resolve_url(context, evidence["operation_id"]),
        json=_resolve_body(context),
        headers=_headers(idempotency_key="resolve-revoked-read-key"),
    )

    assert response.status_code == 202, response.text
    result = response.json()
    assert result["verification_result"] == "unknown"
    assert result["operation"]["status"] == "needs_review"
    assert result["operation"]["evidence"]["verification_result"] == "unknown"
    assert result["run"]["status"] == "needs_review"
    assert "resume" not in result["run"]["available_actions"]
    assert observed_context == [
        (
            str(context["workspace"]),
            context["conversation_id"],
            NEW_SESSION_ID,
            PROFILE_ID,
            "desktop-current",
            "desktop",
            "True",
        )
    ]


def test_resolve_rejects_opaque_stale_unauthorized_and_skip_requests(
    runs_api_context, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    context = runs_api_context
    opaque = _prepare_opaque_recovery(context)
    opaque_url = _resolve_url(context, opaque["operation_id"])
    verifier_calls: list[dict] = []

    class NeverVerify:
        def verify(self, operation):
            verifier_calls.append(operation)
            pytest.fail("an opaque operation must never enter file verification")

    import app.agent.file_mutation.verifier as verifier_module

    monkeypatch.setattr(verifier_module, "_DEFAULT_VERIFIER", NeverVerify())
    opaque_response = context["client"].post(
        opaque_url,
        json=_resolve_body(context),
        headers=_headers(idempotency_key="resolve-opaque-key"),
    )
    assert opaque_response.status_code == 409
    assert opaque_response.json()["code"] == "verification_unavailable"
    assert context["journal"].load_operation(context["run"].run_id, opaque["operation_id"])["status"] == "needs_review"
    assert verifier_calls == []

    # A signed profile cannot inspect or resolve another profile's Run.
    hidden = context["client"].post(
        opaque_url,
        json=_resolve_body(context),
        headers=_headers(profile_id="another-profile", idempotency_key="resolve-other-profile"),
    )
    assert hidden.status_code == 404
    no_profile = context["client"].post(
        opaque_url,
        json=_resolve_body(context),
        headers=_headers(profile_id=None, idempotency_key="resolve-no-profile"),
    )
    assert no_profile.status_code == 403


def test_resolve_stale_revision_workspace_change_and_skip_do_not_verify(
    runs_api_context, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    context = runs_api_context
    evidence = _prepare_verifiable_recovery(context, tmp_path, operation_id="resolve-fence-operation")
    evidence["target"].write_bytes(evidence["after"])
    verifier = _use_real_verifier(monkeypatch, evidence["store"])
    verification_calls = 0
    original_verify = verifier.verify

    def count_verify(operation):
        nonlocal verification_calls
        verification_calls += 1
        return original_verify(operation)

    monkeypatch.setattr(verifier, "verify", count_verify)
    url = _resolve_url(context, evidence["operation_id"])
    current = context["journal"].snapshot(context["run"].run_id)

    stale = context["client"].post(
        url,
        json=_resolve_body(context, revision=int(current["revision"]) - 1),
        headers=_headers(idempotency_key="resolve-stale-key"),
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "revision_conflict"
    assert verification_calls == 0
    assert context["journal"].load_operation(context["run"].run_id, evidence["operation_id"])["status"] == "needs_review"

    skip = context["client"].post(
        url,
        json=_resolve_body(context, resolution="skip"),
        headers=_headers(idempotency_key="resolve-skip-key"),
    )
    assert skip.status_code == 409
    assert skip.json()["code"] == "skip_unavailable"
    assert verification_calls == 0


def test_resolve_workspace_changed_during_real_verification_cannot_mark_operation_verified(
    runs_api_context, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    context = runs_api_context
    evidence = _prepare_verifiable_recovery(context, tmp_path, operation_id="resolve-workspace-race-operation")
    evidence["target"].write_bytes(evidence["after"])
    verifier = _use_real_verifier(monkeypatch, evidence["store"])
    original_verify = verifier.verify
    replacement_workspace = context["workspace"].parent / "workspace-after-verification"
    replacement_workspace.mkdir()

    def change_workspace_after_read(operation):
        result = original_verify(operation)
        assert result["verification_result"] == "completed"
        context["db"].update_conversation(context["conversation_id"], workspace_dir=str(replacement_workspace))
        return result

    monkeypatch.setattr(verifier, "verify", change_workspace_after_read)
    response = context["client"].post(
        _resolve_url(context, evidence["operation_id"]),
        json=_resolve_body(context),
        headers=_headers(idempotency_key="resolve-workspace-race-key"),
    )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "workspace_changed"
    operation = context["journal"].load_operation(context["run"].run_id, evidence["operation_id"])
    assert operation["status"] == "needs_review"
    assert context["journal"].snapshot(context["run"].run_id)["status"] == "needs_review"


def test_parallel_resolve_requests_with_one_idempotency_key_verify_once(
    runs_api_context, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    context = runs_api_context
    evidence = _prepare_verifiable_recovery(context, tmp_path, operation_id="resolve-parallel-operation")
    evidence["target"].write_bytes(evidence["after"])
    verifier = _use_real_verifier(monkeypatch, evidence["store"])
    verify_entered = threading.Event()
    verify_release = threading.Event()
    original_verify = verifier.verify
    calls = 0

    def block_verify(operation):
        nonlocal calls
        calls += 1
        verify_entered.set()
        assert verify_release.wait(timeout=5)
        return original_verify(operation)

    monkeypatch.setattr(verifier, "verify", block_verify)
    body = _resolve_body(context)
    headers = _headers(idempotency_key="resolve-parallel-key")
    url = _resolve_url(context, evidence["operation_id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(context["client"].post, url, json=body, headers=headers)
        assert verify_entered.wait(timeout=5)
        second = pool.submit(context["client"].post, url, json=body, headers=headers)
        time.sleep(0.15)
        verify_release.set()
        responses = [first.result(timeout=10), second.result(timeout=10)]

    assert [response.status_code for response in responses] == [202, 202], [r.text for r in responses]
    assert responses[0].json() == responses[1].json()
    assert calls == 1
    assert context["journal"].load_operation(context["run"].run_id, evidence["operation_id"])["status"] == "verified"


def test_pause_revision_conflict_and_pending_approval_invalidation(runs_api_context, monkeypatch: pytest.MonkeyPatch):
    context = runs_api_context
    run = context["run"]

    class LiveLifecycle:
        durable_run = run

        @property
        def pause_requested(self):
            return run.pause_requested

        async def request_pause(self):
            run.begin_pause()

    lifecycle = LiveLifecycle()
    from app.agent import durable

    durable.turn_run.register_active_lifecycle(run.run_id, lifecycle)
    ticket = create_ticket(
        conversation_id=context["conversation_id"],
        control_session_id=OLD_SESSION_ID,
        execution_source="desktop",
        principal_id=PROFILE_ID,
        permission_profile_id="desktop-current",
        action_type="write",
        tool_name="test_write",
        payload={"input_str": "{}"},
    )

    after_restart = context["client"].post(
        f"/api/approvals/{ticket.id}/approve",
        headers=_headers(session_id=NEW_SESSION_ID),
    )
    assert after_restart.status_code == 403
    assert ticket.status == TicketStatus.PENDING

    # An old revision cannot change run state.
    wrong_session = context["client"].post(
        f"/api/runs/{run.run_id}/pause",
        json={"expected_revision": run.revision, "expected_attempt": run.attempt},
        headers=_headers(session_id=NEW_SESSION_ID, idempotency_key="pause-new-session-denied"),
    )
    assert wrong_session.status_code == 409
    assert wrong_session.json()["code"] == "run_not_live"
    assert run.pause_requested is False

    stale = context["client"].post(
        f"/api/runs/{run.run_id}/pause",
        json={"expected_revision": run.revision + 1, "expected_attempt": run.attempt},
        headers=_headers(session_id=OLD_SESSION_ID, idempotency_key="pause-stale"),
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "revision_conflict"
    assert ticket.status == TicketStatus.PENDING

    context_tokens = [
        (reset_current_conversation_id, set_current_conversation_id(context["conversation_id"])),
        (reset_current_control_session_id, set_current_control_session_id(OLD_SESSION_ID)),
        (reset_current_execution_source, set_current_execution_source("desktop")),
    ]

    async def wait_until_pause():
        gate = ExecutionGateService(default_timeout_seconds=3)
        pending = gate.await_ticket_resolution(
            budget=IterationBudget(max_iterations=1),
            ticket_id=ticket.id,
            event_kind="approval_required",
            tool_dict={"name": "test_write"},
            arguments={},
            execute_tool=lambda *_args: asyncio.sleep(0, result='{"status":"ok"}'),
            pause_requested=lambda: lifecycle.pause_requested,
        )
        task = asyncio.create_task(_collect(pending))
        await asyncio.sleep(0.05)
        current = context["journal"].snapshot(run.run_id)
        paused = context["client"].post(
            f"/api/runs/{run.run_id}/pause",
            json={"expected_revision": current["revision"], "expected_attempt": current["attempt"]},
            headers=_headers(session_id=OLD_SESSION_ID, idempotency_key="pause-pending-approval"),
        )
        assert paused.status_code == 202, paused.text
        events = await asyncio.wait_for(task, timeout=2)
        return paused, events

    try:
        paused, events = asyncio.run(wait_until_pause())
    finally:
        for reset, token in reversed(context_tokens):
            reset(token)
        durable.turn_run._ACTIVE_LIFECYCLES.pop(run.run_id, None)

    assert ticket.status == TicketStatus.REJECTED
    assert json.loads(events[-1]["_result"])["reason_code"] == "pause_requested"
    old_approval = context["client"].post(
        f"/api/approvals/{ticket.id}/approve",
        headers=_headers(session_id=OLD_SESSION_ID),
    )
    assert old_approval.status_code == 409
    assert get_ticket(ticket.id).status == TicketStatus.REJECTED
    assert paused.json()["run"]["status"] == "pausing"


async def _collect(generator) -> list[dict]:
    return [event async for event in generator]


def test_event_replay_is_paged_and_rejects_invalid_or_expired_cursors(runs_api_context):
    context = runs_api_context
    journal = context["journal"]
    run_id = context["run"].run_id
    initial = context["client"].get(f"/api/runs/{run_id}/events?after_seq=0&limit=1", headers=_headers())
    assert initial.status_code == 200, initial.text
    page = initial.json()
    assert [event["seq"] for event in page["events"]] == [1]
    assert page["events"][0]["type"] == "run.created"

    latest = journal.snapshot(run_id)
    beyond = context["client"].get(
        f"/api/runs/{run_id}/events?after_seq={latest['last_seq'] + 1}", headers=_headers()
    )
    assert beyond.status_code == 400
    assert beyond.json()["code"] == "invalid_cursor"

    journal.db.execute("DELETE FROM run_events WHERE run_id = ? AND seq = 1", (run_id,))
    journal.db.commit()
    expired = context["client"].get(
        f"/api/runs/{run_id}/events?after_seq=0", headers=_headers()
    )
    assert expired.status_code == 410
    assert expired.json()["code"] == "events_expired"
    expired_stream = context["client"].get(
        f"/api/runs/{run_id}/events/stream?after_seq=0", headers=_headers()
    )
    assert expired_stream.status_code == 410
    assert expired_stream.json()["code"] == "events_expired"

    negative = context["client"].get(
        f"/api/runs/{run_id}/events?after_seq=-1", headers=_headers()
    )
    assert negative.status_code == 400
    assert negative.json()["code"] == "invalid_cursor"


def test_event_follow_catches_event_written_between_replay_and_subscription(runs_api_context):
    context = runs_api_context
    journal = context["journal"]
    run = context["run"]

    async def scenario():
        stream = journal.event_store.follow_events(
            run.run_id, after_seq=0, poll_interval=0.001, page_size=10, max_events=2
        )
        first = await anext(stream)
        assert first["seq"] == 1
        operation = journal.prepare_operation(
            run.run_id,
            operation_id="follow-race-operation",
            owner_instance_id=run.owner_instance_id,
            attempt=run.attempt,
            operation_kind="tool_call",
            effect_class="read_only",
        )
        second = await anext(stream)
        await stream.aclose()
        return first, operation, second

    first, operation, second = asyncio.run(scenario())
    assert second["seq"] == first["seq"] + 1
    assert second["type"] == "operation.prepared"
    assert second["payload"]["operation_id"] == operation["operation_id"]


def test_http_sse_replays_then_follows_events_written_at_the_subscription_boundary(runs_api_context):
    from app.api.routes import runs

    context = runs_api_context
    journal = context["journal"]
    run = context["run"]

    class RequestStub:
        state = SimpleNamespace(
            control_session=SimpleNamespace(
                session_id=NEW_SESSION_ID,
                profile_id=PROFILE_ID,
                expires_at=4_000_000_000,
            )
        )

        async def is_disconnected(self):
            return False

    async def scenario():
        response = await runs.stream_run_events(run.run_id, RequestStub(), after_seq=0)
        body_iterator = response.body_iterator
        first = await anext(body_iterator)
        assert b"id: 1\nevent: run.created" in first
        operation = journal.prepare_operation(
            run.run_id,
            operation_id="http-sse-boundary-operation",
            owner_instance_id=run.owner_instance_id,
            attempt=run.attempt,
            operation_kind="tool_call",
            effect_class="read_only",
        )
        second = await asyncio.wait_for(anext(body_iterator), timeout=2)
        await body_iterator.aclose()
        return operation, second

    operation, event_chunk = asyncio.run(scenario())
    decoded = event_chunk.decode("utf-8")
    assert "id: 2\nevent: operation.prepared" in decoded
    assert operation["operation_id"] in decoded


def test_slow_subscriber_reconnects_from_last_delivered_sequence_without_gaps(runs_api_context):
    context = runs_api_context
    journal = context["journal"]
    run = context["run"]
    last_delivered = 1
    for index in range(260):
        operation = journal.prepare_operation(
            run.run_id,
            operation_id=f"slow-subscriber-{index}",
            owner_instance_id=run.owner_instance_id,
            attempt=run.attempt,
            operation_kind="tool_call",
            effect_class="read_only",
        )
        journal.transition_operation(
            run.run_id,
            operation["operation_id"],
            to_status="authorized",
            owner_instance_id=run.owner_instance_id,
            attempt=run.attempt,
        )

    first = context["client"].get(
        f"/api/runs/{run.run_id}/events?after_seq={last_delivered}&limit=500", headers=_headers()
    )
    assert first.status_code == 200, first.text
    first_page = first.json()
    assert len(first_page["events"]) == 500
    assert first_page["events"][0]["seq"] == last_delivered + 1
    assert first_page["next_seq"] == first_page["events"][-1]["seq"]

    follow = context["client"].get(
        f"/api/runs/{run.run_id}/events?after_seq={first_page['next_seq']}&limit=500", headers=_headers()
    )
    assert follow.status_code == 200, follow.text
    remainder = follow.json()
    all_sequences = [event["seq"] for event in first_page["events"] + remainder["events"]]
    assert all_sequences == list(range(last_delivered + 1, int(remainder["last_seq"]) + 1))
