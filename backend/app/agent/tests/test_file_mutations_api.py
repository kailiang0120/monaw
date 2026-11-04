"""HTTP acceptance coverage for file mutation preview and Apply."""

from __future__ import annotations

import os
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.agent.database import get_db
from app.agent.durable.journal import Journal
from app.agent.durable.operation_execution import (
    prepare_tool_operation,
    reset_current_operation_execution,
    set_current_operation_execution,
)
from app.agent.durable.turn_run import create_durable_run
from app.agent.file_mutation.snapshot_store import SnapshotStore
from app.agent.run_context import (
    reset_current_control_session_id,
    reset_current_conversation_id,
    reset_current_principal_id,
    set_current_control_session_id,
    set_current_conversation_id,
    set_current_principal_id,
)
from app.agent.settings_store import AgentSettings, PathRule, save_agent_settings
from app.main import app
from app.security.control_plane import ALL_SCOPES, mint_control_token
from app.skills.filesystem import file_ops


@pytest.fixture
def file_mutation_api_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import app.agent.controller_policy as controller_policy

    root = tmp_path / "workspace"
    root.mkdir()
    policy_dir = tmp_path / "policy"
    policy_dir.mkdir()
    monkeypatch.setattr(controller_policy, "_POLICY_DIR", policy_dir)
    monkeypatch.setattr(controller_policy, "_POLICY_FILE", policy_dir / "controller_policy.md")
    monkeypatch.setattr(controller_policy, "_ALLOWLIST_FILE", policy_dir / "allowlisted_apps.md")

    session_id = "session-file-mutation-api"
    conversation_id = "chat-file-mutation-api"
    run_id = "run-file-mutation-api"
    token = mint_control_token(
        os.environ["MONAW_CONTROL_SECRET"],
        scopes=ALL_SCOPES,
        session_id=session_id,
    )
    db = get_db()
    db.create_conversation(conversation_id, "File mutation API acceptance", str(root))
    run = create_durable_run(
        conversation_id=conversation_id,
        execution_source="desktop",
        principal_id=session_id,
        control_session_id=session_id,
        workspace_dir=str(root),
        run_id=run_id,
        turn_id="turn-file-mutation-api",
        db=db,
    )

    settings = AgentSettings()
    settings.permissions.mode = "full_access"
    settings.permissions.blocked_roots = []
    settings.permissions.path_rules = [
        PathRule(path=str(root), read=True, write=True, delete=True, launch=False)
    ]
    settings.permissions.allow_delete = True
    settings.permissions.confirmations.mutate = False
    settings.permissions.confirmations.delete = False
    settings.permissions.dangerous_actions_require_confirm = False
    settings_path = policy_dir.parent / "settings.json"
    save_agent_settings(settings, settings_path=settings_path)

    store = SnapshotStore(tmp_path / "snapshots")
    monkeypatch.setattr(file_ops, "_p3_snapshot_store", lambda: store)
    from app.agent.file_mutation import undo_manager

    monkeypatch.setattr(undo_manager, "get_snapshot_store", lambda: store)
    from app.api.routes import file_mutations

    lifecycle = SimpleNamespace(
        durable_run=run,
        pause_requested=False,
        review_requested=False,
    )
    monkeypatch.setattr(
        file_mutations,
        "_active_lifecycle_for_run",
        lambda requested_run_id: lifecycle if requested_run_id == run_id else None,
    )
    with file_mutations._RECORD_LOCK:
        file_mutations._PREVIEWS.clear()
        file_mutations._IDEMPOTENCY.clear()
    client = TestClient(app)
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "file-mutation-preview-1",
    }
    yield {
        "client": client,
        "db": db,
        "headers": headers,
        "root": root,
        "conversation_id": conversation_id,
        "run_id": run_id,
        "session_id": session_id,
        "token": token,
        "store": store,
        "run": run,
        "settings_path": settings_path,
    }


def _preview_body(context, path: Path, *, content: str = "after image\n") -> dict[str, object]:
    revision = Journal(context["db"]).snapshot(context["run_id"])["revision"]
    return {
        "run_id": context["run_id"],
        "conversation_id": context["conversation_id"],
        "expected_revision": revision,
        "operation": "write",
        "path": str(path),
        "content": content,
        "overwrite": True,
        "create_parents": False,
        "encoding": "utf-8",
    }


def test_preview_then_external_edit_makes_apply_stale_without_overwriting(
    file_mutation_api_context,
):
    context = file_mutation_api_context
    target = context["root"] / "stale.txt"
    target.write_bytes(b"before image\n")

    preview = context["client"].post(
        "/api/file-mutations/preview",
        json=_preview_body(context, target),
        headers=context["headers"],
    )

    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["mutation_id"]
    assert body["undo_capability"] == "available"
    assert target.read_bytes() == b"before image\n"
    assert "before image" in body["preview"].get("text", "")

    target.write_bytes(b"external user edit C\n")
    apply = context["client"].post(
        f"/api/file-mutations/{body['mutation_id']}/apply",
        json={"expected_revision": body["revision"]},
        headers={**context["headers"], "Idempotency-Key": "file-mutation-apply-1"},
    )

    assert apply.status_code == 409
    assert apply.json()["code"] == "stale_preview"
    assert target.read_bytes() == b"external user edit C\n"


def test_preview_idempotency_returns_same_mutation_and_rejects_changed_payload(
    file_mutation_api_context,
):
    context = file_mutation_api_context
    target = context["root"] / "idempotent-preview.txt"
    target.write_bytes(b"before\n")
    headers = {**context["headers"], "Idempotency-Key": "same-preview-key"}
    body = _preview_body(context, target)

    first = context["client"].post("/api/file-mutations/preview", json=body, headers=headers)
    retry = context["client"].post("/api/file-mutations/preview", json=body, headers=headers)

    assert first.status_code == retry.status_code == 200
    assert first.json()["mutation_id"] == retry.json()["mutation_id"]

    conflicting = context["client"].post(
        "/api/file-mutations/preview",
        json=_preview_body(context, target, content="different payload\n"),
        headers=headers,
    )

    assert conflicting.status_code == 409
    assert conflicting.json()["code"] == "idempotency_conflict"
    assert target.read_bytes() == b"before\n"


def test_cached_preview_rechecks_read_permission_before_returning_diff(
    file_mutation_api_context,
):
    context = file_mutation_api_context
    target = context["root"] / "read-revoked.txt"
    target.write_bytes(b"private before image\n")
    headers = {**context["headers"], "Idempotency-Key": "read-revocation-key"}
    body = _preview_body(context, target)
    first = context["client"].post("/api/file-mutations/preview", json=body, headers=headers)
    assert first.status_code == 200, first.text
    assert "private before image" in first.text

    settings = AgentSettings.model_validate_json(context["settings_path"].read_text("utf-8"))
    settings.permissions.path_rules = [
        PathRule(path=str(context["root"]), read=False, write=True, delete=True, launch=False)
    ]
    save_agent_settings(settings, settings_path=context["settings_path"])

    retry = context["client"].post("/api/file-mutations/preview", json=body, headers=headers)

    assert retry.status_code in {403, 409}
    assert retry.json()["code"] in {
        "permission_denied",
        "read_permission_required",
        "stale_preview",
    }
    assert "private before image" not in retry.text
    assert target.read_bytes() == b"private before image\n"


def test_apply_retry_with_original_revision_and_key_returns_original_result(
    file_mutation_api_context,
):
    context = file_mutation_api_context
    target = context["root"] / "apply-idempotent.txt"
    target.write_bytes(b"before image\n")
    preview = context["client"].post(
        "/api/file-mutations/preview",
        json=_preview_body(context, target),
        headers={**context["headers"], "Idempotency-Key": "apply-idempotent-preview"},
    )
    assert preview.status_code == 200, preview.text
    mutation_id = preview.json()["mutation_id"]
    apply_headers = {**context["headers"], "Idempotency-Key": "apply-idempotent-retry"}
    apply_body = {"expected_revision": preview.json()["revision"]}

    first = context["client"].post(
        f"/api/file-mutations/{mutation_id}/apply", json=apply_body, headers=apply_headers
    )
    retry = context["client"].post(
        f"/api/file-mutations/{mutation_id}/apply", json=apply_body, headers=apply_headers
    )

    assert first.status_code == retry.status_code == 200, (first.text, retry.text)
    assert first.json() == retry.json()
    assert first.json()["operation_id"]
    assert target.read_bytes() == b"after image\n"
    operation_rows = context["run"].journal.list_operations(context["run_id"])
    assert sum(row["operation_kind"] == "file_write" for row in operation_rows) == 1


def test_concurrent_preview_with_same_key_returns_one_mutation_id(
    file_mutation_api_context, monkeypatch: pytest.MonkeyPatch
):
    from app.api.routes import file_mutations

    context = file_mutation_api_context
    target = context["root"] / "concurrent-preview.txt"
    target.write_bytes(b"before\n")
    original_plan = file_mutations._plan_write
    original_key = file_mutations._preview_key
    plan_entered = 0
    preview_keys = []
    plan_lock = threading.Lock()

    def track_key(*args, **kwargs):
        value = original_key(*args, **kwargs)
        with plan_lock:
            preview_keys.append(value)
        return value

    def delayed_plan(*args, **kwargs):
        nonlocal plan_entered
        with plan_lock:
            plan_entered += 1
        time.sleep(0.05)
        return original_plan(*args, **kwargs)

    monkeypatch.setattr(file_mutations, "_plan_write", delayed_plan)
    monkeypatch.setattr(file_mutations, "_preview_key", track_key)
    request_barrier = threading.Barrier(3)
    body = _preview_body(context, target)
    headers = {**context["headers"], "Idempotency-Key": "concurrent-preview-key"}

    def post_preview():
        request_barrier.wait(timeout=5)
        return context["client"].post(
            "/api/file-mutations/preview", json=body, headers=headers
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(post_preview)
        second_future = pool.submit(post_preview)
        request_barrier.wait(timeout=5)
        responses = [first_future.result(timeout=10), second_future.result(timeout=10)]

    assert [response.status_code for response in responses] == [200, 200]
    assert len(preview_keys) == 2 and preview_keys[0] == preview_keys[1], preview_keys
    assert responses[0].json()["mutation_id"] == responses[1].json()["mutation_id"], (
        responses[0].json(), responses[1].json(),
        {"plan_entered": plan_entered, "preview_keys": preview_keys},
    )
    assert plan_entered == 1, {"plan_entered": plan_entered}
    assert target.read_bytes() == b"before\n"


def test_other_control_session_cannot_preview_a_run_or_read_its_file(
    file_mutation_api_context,
):
    context = file_mutation_api_context
    target = context["root"] / "session-private.txt"
    target.write_bytes(b"secret before image\n")
    other_token = mint_control_token(
        os.environ["MONAW_CONTROL_SECRET"],
        scopes=ALL_SCOPES,
        session_id="different-control-session",
    )

    response = context["client"].post(
        "/api/file-mutations/preview",
        json=_preview_body(context, target),
        headers={
            "Authorization": f"Bearer {other_token}",
            "Idempotency-Key": "unauthorized-preview",
        },
    )

    assert response.status_code == 403
    assert "secret before image" not in response.text
    assert target.read_bytes() == b"secret before image\n"


def test_changed_conversation_workspace_invalidates_preview_before_file_access(
    file_mutation_api_context,
):
    context = file_mutation_api_context
    target = context["root"] / "workspace-private.txt"
    target.write_bytes(b"workspace before image\n")
    changed_workspace = context["root"].parent / "replacement-workspace"
    changed_workspace.mkdir()
    context["db"].update_conversation(
        context["conversation_id"], workspace_dir=str(changed_workspace)
    )

    response = context["client"].post(
        "/api/file-mutations/preview",
        json=_preview_body(context, target),
        headers={**context["headers"], "Idempotency-Key": "workspace-changed"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "workspace_changed"
    assert "workspace before image" not in response.text
    assert target.read_bytes() == b"workspace before image\n"


def _create_agent_verified_write(context, path: Path) -> str:
    conversation_token = set_current_conversation_id(context["conversation_id"])
    session_token = set_current_control_session_id(context["session_id"])
    principal_token = set_current_principal_id(context["session_id"])
    operation = prepare_tool_operation(
        context["run"], tool_name="fs_write", trusted_builtin=True
    )
    assert operation is not None
    operation.begin_effect()
    execution_token = set_current_operation_execution(operation)
    try:
        raw = file_ops.file_write(str(path), "agent after image\n", overwrite=True)
    finally:
        reset_current_operation_execution(execution_token)
        reset_current_principal_id(principal_token)
        reset_current_control_session_id(session_token)
        reset_current_conversation_id(conversation_token)
    operation.note_pending_preflight(raw)
    operation.settle(raw)
    result = json.loads(raw)
    assert result["status"] == "ok", result
    row = context["run"].journal.load_operation_internal(
        context["run_id"], operation.operation_id
    )
    assert row is not None and row["status"] == "verified"
    return operation.operation_id


def _finish_api_run(context) -> None:
    run = context["run"]
    snapshot = run.journal.snapshot(run.run_id)
    if snapshot["status"] in {"completed", "failed", "cancelled"}:
        return
    run.journal.transition_run(
        run.run_id,
        to_status="failed",
        owner_instance_id=run.owner_instance_id,
        attempt=run.attempt,
        expected_revision=snapshot["revision"],
        reason_code="run_failed",
    )


def test_agent_verified_operation_can_be_undone_after_preview_cache_is_cleared(
    file_mutation_api_context,
):
    from app.api.routes import file_mutations

    context = file_mutation_api_context
    target = context["root"] / "agent-operation.txt"
    before = b"agent before image\n"
    target.write_bytes(before)
    operation_id = _create_agent_verified_write(context, target)
    _finish_api_run(context)
    with file_mutations._RECORD_LOCK:
        file_mutations._PREVIEWS.clear()
        file_mutations._IDEMPOTENCY.clear()
    expected_revision = Journal(context["db"]).snapshot(context["run_id"])["revision"]

    response = context["client"].post(
        f"/api/file-mutations/{operation_id}/undo",
        json={
            "run_id": context["run_id"],
            "conversation_id": context["conversation_id"],
            "expected_revision": expected_revision,
        },
        headers={**context["headers"], "Idempotency-Key": "undo-agent-operation"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "undone"
    assert response.json()["operation_id"] == operation_id
    assert target.read_bytes() == before
