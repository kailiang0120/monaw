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
        file_mutations._UNDO_CONFIRMATIONS.clear()
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


def test_git_changes_preview_confirmation_and_conflict(file_mutation_api_context, monkeypatch):
    from app.agent import git_changes
    context = file_mutation_api_context
    monkeypatch.setattr(git_changes, "STORE", context["root"].parent / "git-api")
    target = context["root"] / "patched.txt"
    target.write_bytes(b"user edit\n")
    git_changes.checkpoint(context["run_id"], str(context["root"]), "before")
    target.write_bytes(b"agent edit\n")
    (context["root"] / "command.txt").write_bytes(b"command created this\n")
    context["run"].finalize("error")
    url = f'/api/runs/{context["run_id"]}/changes'
    client, headers = context["client"], context["headers"]
    params = {"conversation_id": context["conversation_id"]}
    result = client.get(url, params=params, headers=headers)
    assert result.status_code == 200, result.text
    assert {f["path"] for f in result.json()["files"]} == {"patched.txt", "command.txt"}, result.text
    other_token = mint_control_token(os.environ["MONAW_CONTROL_SECRET"], scopes=ALL_SCOPES, session_id="another-owner")
    assert client.get(url, params=params, headers={"Authorization": f"Bearer {other_token}"}).status_code == 403
    limited_token = mint_control_token(os.environ["MONAW_CONTROL_SECRET"], scopes=["runs:read", "runs:control"], session_id=context["session_id"])
    assert client.get(url, params=params, headers={"Authorization": f"Bearer {limited_token}"}).status_code == 403
    body = {**params, "path": "patched.txt"}
    preview = client.post(url + "/undo-preview", json=body, headers=headers)
    assert preview.status_code == 200, preview.text
    assert "+user edit" in preview.json()["file"]["diff"]
    assert target.read_bytes() == b"agent edit\n"
    forged = client.post(url + "/undo", json={**body, "confirmation_id": "fake"}, headers=headers)
    assert forged.status_code == 409
    result = client.post(url + "/undo", json={**body, "confirmation_id": preview.json()["confirmation_id"]}, headers=headers)
    assert result.status_code == 200, result.text
    assert target.read_bytes() == b"user edit\n"
    body["path"] = "command.txt"
    preview = client.post(url + "/undo-preview", json=body, headers=headers)
    if os.name == "nt":
        assert preview.status_code == 200, preview.text
        (context["root"] / "command.txt").write_bytes(b"external edit\n")
        result = client.post(url + "/undo", json={**body, "confirmation_id": preview.json()["confirmation_id"]}, headers=headers)
        assert result.status_code == 409
        assert (context["root"] / "command.txt").read_bytes() == b"external edit\n"


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


def _request_undo_preview(context, operation_id: str, *, token: str | None = None):
    revision = Journal(context["db"]).snapshot(context["run_id"])["revision"]
    return context["client"].post(
        f"/api/file-mutations/{operation_id}/undo-preview",
        json={
            "run_id": context["run_id"],
            "conversation_id": context["conversation_id"],
            "expected_revision": revision,
        },
        headers={
            "Authorization": f"Bearer {token}" if token else f"Bearer {context['token']}",
        },
    )


def _confirm_undo(context, operation_id: str, confirmation_id: str, *, key: str = "undo-confirmed", token: str | None = None):
    revision = Journal(context["db"]).snapshot(context["run_id"])["revision"]
    return context["client"].post(
        f"/api/file-mutations/{operation_id}/undo",
        json={
            "run_id": context["run_id"],
            "conversation_id": context["conversation_id"],
            "expected_revision": revision,
            "confirmation_id": confirmation_id,
        },
        headers={
            "Authorization": f"Bearer {token}" if token else f"Bearer {context['token']}",
            "Idempotency-Key": key,
        },
    )


def _set_default_mode_for_undo(context, *, write: bool = True) -> None:
    settings = AgentSettings()
    settings.permissions.mode = "default"
    settings.permissions.blocked_roots = []
    settings.permissions.path_rules = [
        PathRule(path=str(context["root"]), read=True, write=write, delete=False, launch=False)
    ]
    settings.permissions.allow_delete = False
    settings.permissions.confirmations.mutate = True
    save_agent_settings(settings, settings_path=context["settings_path"])


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
    preview = _request_undo_preview(context, operation_id)
    assert preview.status_code == 200, preview.text
    response = _confirm_undo(context, operation_id, preview.json()["confirmation_id"], key="undo-agent-operation")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "undone"
    assert response.json()["operation_id"] == operation_id
    assert target.read_bytes() == before


def test_undo_capability_checks_conflict_and_completed_undo(file_mutation_api_context):
    context = file_mutation_api_context
    target = context["root"] / "undo-capability.txt"
    target.write_bytes(b"before\n")
    operation_id = _create_agent_verified_write(context, target)
    _finish_api_run(context)
    query = {"run_id": context["run_id"], "conversation_id": context["conversation_id"]}
    route = f"/api/file-mutations/{operation_id}/undo-capability"

    available = context["client"].get(route, params=query, headers=context["headers"])
    assert available.status_code == 200
    assert available.json()["status"] == "available"
    assert "path" not in available.json()
    target.write_bytes(b"external edit\n")
    conflict = context["client"].get(route, params=query, headers=context["headers"])
    assert conflict.json()["status"] == "conflict"
    assert target.read_bytes() == b"external edit\n"


def test_default_mode_uses_separate_confirmation_to_remove_only_the_created_file(
    file_mutation_api_context,
):
    context = file_mutation_api_context
    target = context["root"] / "created-in-default-mode.txt"
    operation_id = _create_agent_verified_write(context, target)
    _finish_api_run(context)
    _set_default_mode_for_undo(context)

    query = {"run_id": context["run_id"], "conversation_id": context["conversation_id"]}
    capability = context["client"].get(
        f"/api/file-mutations/{operation_id}/undo-capability",
        params=query,
        headers=context["headers"],
    )
    assert capability.status_code == 200
    assert capability.json()["status"] == "available"

    preview = _request_undo_preview(context, operation_id)
    assert preview.status_code == 200, preview.text
    preview_body = preview.json()
    assert preview_body["files"][0]["path"] == str(target)
    assert preview_body["files"][0]["action"] == "remove"
    assert preview_body["files"][0]["before"]["exists"] is True
    assert preview_body["files"][0]["after"]["exists"] is False
    assert target.read_bytes() == b"agent after image\n"

    confirmed = _confirm_undo(
        context, operation_id, preview_body["confirmation_id"], key="default-mode-undo"
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "undone"
    assert not target.exists()


def test_undo_preview_can_be_declined_and_client_cannot_forge_confirmation(
    file_mutation_api_context,
):
    context = file_mutation_api_context
    target = context["root"] / "declined-undo.txt"
    target.write_bytes(b"before\n")
    operation_id = _create_agent_verified_write(context, target)
    _finish_api_run(context)

    preview = _request_undo_preview(context, operation_id)
    assert preview.status_code == 200, preview.text
    assert target.read_bytes() == b"agent after image\n"
    assert not [
        row for row in Journal(context["db"]).list_operations(context["run_id"])
        if row["operation_kind"] == "undo"
    ]

    forged = _confirm_undo(context, operation_id, "x" * 64, key="forged-confirmation")
    assert forged.status_code == 409
    assert forged.json()["code"] == "undo_confirmation_expired"
    assert target.read_bytes() == b"agent after image\n"


def test_expired_undo_confirmation_leaves_file_untouched(file_mutation_api_context):
    from app.api.routes import file_mutations

    context = file_mutation_api_context
    target = context["root"] / "expired-undo-confirmation.txt"
    target.write_bytes(b"before\n")
    operation_id = _create_agent_verified_write(context, target)
    _finish_api_run(context)
    preview = _request_undo_preview(context, operation_id)
    assert preview.status_code == 200, preview.text
    confirmation_id = preview.json()["confirmation_id"]
    with file_mutations._RECORD_LOCK:
        file_mutations._UNDO_CONFIRMATIONS[confirmation_id].expires_at = time.monotonic() - 1

    response = _confirm_undo(context, operation_id, confirmation_id, key="expired-confirmation")
    assert response.status_code == 409
    assert response.json()["code"] == "undo_confirmation_expired"
    assert target.read_bytes() == b"agent after image\n"


def test_permissions_revoked_after_undo_preview_leave_file_untouched(file_mutation_api_context):
    context = file_mutation_api_context
    target = context["root"] / "revoked-undo-permission.txt"
    target.write_bytes(b"before\n")
    operation_id = _create_agent_verified_write(context, target)
    _finish_api_run(context)
    _set_default_mode_for_undo(context)
    preview = _request_undo_preview(context, operation_id)
    assert preview.status_code == 200, preview.text

    _set_default_mode_for_undo(context, write=False)
    response = _confirm_undo(
        context, operation_id, preview.json()["confirmation_id"], key="revoked-undo-permission"
    )
    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"
    assert target.read_bytes() == b"agent after image\n"


def test_external_edit_after_undo_preview_is_preserved(file_mutation_api_context):
    context = file_mutation_api_context
    target = context["root"] / "edited-after-undo-preview.txt"
    target.write_bytes(b"before\n")
    operation_id = _create_agent_verified_write(context, target)
    _finish_api_run(context)
    preview = _request_undo_preview(context, operation_id)
    assert preview.status_code == 200, preview.text
    target.write_bytes(b"user content C\n")

    response = _confirm_undo(
        context, operation_id, preview.json()["confirmation_id"], key="edited-after-undo-preview"
    )
    assert response.status_code == 409
    assert response.json()["code"] == "undo_conflict"
    assert target.read_bytes() == b"user content C\n"


def test_undo_confirmation_is_bound_to_operation_and_revision(file_mutation_api_context):
    context = file_mutation_api_context
    first = context["root"] / "bound-first.txt"
    second = context["root"] / "bound-second.txt"
    first.write_bytes(b"first before\n")
    first_operation = _create_agent_verified_write(context, first)
    second.write_bytes(b"second before\n")
    second_operation = _create_agent_verified_write(context, second)
    _finish_api_run(context)
    preview = _request_undo_preview(context, first_operation)
    assert preview.status_code == 200, preview.text
    confirmation_id = preview.json()["confirmation_id"]

    wrong_operation = _confirm_undo(
        context, second_operation, confirmation_id, key="wrong-operation"
    )
    assert wrong_operation.status_code == 409
    assert wrong_operation.json()["code"] == "undo_confirmation_invalid"
    assert first.read_bytes() == second.read_bytes() == b"agent after image\n"

    body = {
        "run_id": context["run_id"],
        "conversation_id": context["conversation_id"],
        "expected_revision": preview.json()["revision"] + 1,
        "confirmation_id": confirmation_id,
    }
    wrong_revision = context["client"].post(
        f"/api/file-mutations/{first_operation}/undo",
        json=body,
        headers={**context["headers"], "Idempotency-Key": "wrong-revision"},
    )
    assert wrong_revision.status_code == 409
    assert wrong_revision.json()["code"] == "undo_confirmation_invalid"
    assert first.read_bytes() == b"agent after image\n"


def test_workspace_change_rejects_a_pending_undo_confirmation(file_mutation_api_context):
    context = file_mutation_api_context
    target = context["root"] / "workspace-bound-undo.txt"
    target.write_bytes(b"before\n")
    operation_id = _create_agent_verified_write(context, target)
    _finish_api_run(context)
    preview = _request_undo_preview(context, operation_id)
    assert preview.status_code == 200, preview.text

    replacement_workspace = context["root"].parent / "replacement-workspace"
    replacement_workspace.mkdir()
    context["db"].update_conversation(
        context["conversation_id"], workspace_dir=str(replacement_workspace)
    )
    response = _confirm_undo(
        context, operation_id, preview.json()["confirmation_id"], key="changed-workspace"
    )
    assert response.status_code == 409
    assert response.json()["code"] == "workspace_changed"
    assert target.read_bytes() == b"agent after image\n"


def test_repeated_undo_confirmation_claims_only_one_inverse(file_mutation_api_context):
    context = file_mutation_api_context
    target = context["root"] / "duplicate-undo-confirmation.txt"
    target.write_bytes(b"before\n")
    operation_id = _create_agent_verified_write(context, target)
    _finish_api_run(context)
    preview = _request_undo_preview(context, operation_id)
    assert preview.status_code == 200, preview.text
    preview_body = preview.json()
    confirmation_body = {
        "run_id": context["run_id"],
        "conversation_id": context["conversation_id"],
        "expected_revision": preview_body["revision"],
        "confirmation_id": preview_body["confirmation_id"],
    }
    headers = {**context["headers"], "Idempotency-Key": "same-undo-confirmation"}
    route = f"/api/file-mutations/{operation_id}/undo"
    barrier = threading.Barrier(3)

    def submit():
        barrier.wait(timeout=5)
        return context["client"].post(route, json=confirmation_body, headers=headers)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(submit)
        second = pool.submit(submit)
        barrier.wait(timeout=5)
        responses = [first.result(timeout=15), second.result(timeout=15)]

    assert all(response.status_code == 200 for response in responses), [response.text for response in responses]
    assert target.read_bytes() == b"before\n"
    undo_rows = [
        row for row in Journal(context["db"]).list_operations(context["run_id"])
        if row["operation_kind"] == "undo"
    ]
    assert len(undo_rows) == 1
    replay = context["client"].post(route, json=confirmation_body, headers=headers)
    assert replay.status_code == 200
    assert replay.json()["status"] == "undone"
    assert target.read_bytes() == b"before\n"

    another_key = context["client"].post(
        route,
        json=confirmation_body,
        headers={**context["headers"], "Idempotency-Key": "second-confirmation-key"},
    )
    assert another_key.status_code == 409
    assert another_key.json()["code"] == "undo_confirmation_used"
    assert len([
        row for row in Journal(context["db"]).list_operations(context["run_id"])
        if row["operation_kind"] == "undo"
    ]) == 1


def test_undo_capability_reflects_successful_undo(file_mutation_api_context):
    context = file_mutation_api_context
    target = context["root"] / "undo-capability-complete.txt"
    target.write_bytes(b"before\n")
    operation_id = _create_agent_verified_write(context, target)
    _finish_api_run(context)
    query = {"run_id": context["run_id"], "conversation_id": context["conversation_id"]}
    route = f"/api/file-mutations/{operation_id}/undo-capability"
    result = context["client"].get(route, params=query, headers=context["headers"])
    preview = _request_undo_preview(context, operation_id)
    assert preview.status_code == 200, preview.text
    undone = _confirm_undo(context, operation_id, preview.json()["confirmation_id"], key="capability-undo")
    assert undone.status_code == 200, undone.text
    assert context["client"].get(route, params=query, headers=context["headers"]).json()["status"] == "undone"


def test_profile_can_undo_persisted_operation_after_session_changes(
    file_mutation_api_context,
):
    from app.api.routes import file_mutations

    context = file_mutation_api_context
    _finish_api_run(context)
    profile_id = "profile-file-mutation-api"
    old_session_id = context["session_id"]
    conversation_id = "chat-file-mutation-profile"
    context["db"].create_conversation(conversation_id, "Profile-scoped Undo", str(context["root"]))
    run = create_durable_run(
        conversation_id=conversation_id,
        execution_source="desktop",
        principal_id=profile_id,
        control_session_id=old_session_id,
        workspace_dir=str(context["root"]),
        run_id="run-file-mutation-profile",
        turn_id="turn-file-mutation-profile",
        db=context["db"],
    )
    target = context["root"] / "profile-owned-operation.txt"
    before = b"profile before image\n"
    target.write_bytes(before)

    conversation_token = set_current_conversation_id(conversation_id)
    session_token = set_current_control_session_id(old_session_id)
    principal_token = set_current_principal_id(profile_id)
    operation = prepare_tool_operation(run, tool_name="fs_write", trusted_builtin=True)
    assert operation is not None
    operation.begin_effect()
    execution_token = set_current_operation_execution(operation)
    try:
        raw = file_ops.file_write(str(target), "profile after image\n", overwrite=True)
    finally:
        reset_current_operation_execution(execution_token)
        reset_current_principal_id(principal_token)
        reset_current_control_session_id(session_token)
        reset_current_conversation_id(conversation_token)
    operation.note_pending_preflight(raw)
    operation.settle(raw)
    assert json.loads(raw)["status"] == "ok"
    operation_row = run.journal.load_operation_internal(run.run_id, operation.operation_id)
    assert operation_row is not None and operation_row["status"] == "verified"

    _finish_api_run({**context, "run": run, "run_id": run.run_id})
    with file_mutations._RECORD_LOCK:
        # Simulate the in-memory preview/idempotency cache being lost with the
        # old desktop session; the operation and snapshot remain durable.
        file_mutations._PREVIEWS.clear()
        file_mutations._IDEMPOTENCY.clear()

    new_session_id = "session-after-profile-restart"
    profile_token = mint_control_token(
        os.environ["MONAW_CONTROL_SECRET"],
        scopes=ALL_SCOPES,
        session_id=new_session_id,
        profile_id=profile_id,
    )
    # This helper uses the main context IDs, so build the profile Run's preview
    # directly under the new signed profile and preserve the returned binding.
    expected_revision = Journal(context["db"]).snapshot(run.run_id)["revision"]
    preview_response = context["client"].post(
        f"/api/file-mutations/{operation.operation_id}/undo-preview",
        json={
            "run_id": run.run_id,
            "conversation_id": conversation_id,
            "expected_revision": expected_revision,
        },
        headers={"Authorization": f"Bearer {profile_token}"},
    )
    assert preview_response.status_code == 200, preview_response.text
    confirmation_id = preview_response.json()["confirmation_id"]
    undo_body = {
        "run_id": run.run_id,
        "conversation_id": conversation_id,
        "expected_revision": expected_revision,
        "confirmation_id": confirmation_id,
    }

    other_profile_token = mint_control_token(
        os.environ["MONAW_CONTROL_SECRET"],
        scopes=ALL_SCOPES,
        session_id="other-profile-session",
        profile_id="profile-file-mutation-other",
    )
    denied = context["client"].post(
        f"/api/file-mutations/{operation.operation_id}/undo",
        json=undo_body,
        headers={
            "Authorization": f"Bearer {other_profile_token}",
            "Idempotency-Key": "other-profile-undo",
        },
    )
    assert denied.status_code == 403
    assert target.read_bytes() != before

    response = context["client"].post(
        f"/api/file-mutations/{operation.operation_id}/undo",
        json=undo_body,
        headers={
            "Authorization": f"Bearer {profile_token}",
            "Idempotency-Key": "same-profile-after-restart-undo",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "undone"
    assert target.read_bytes() == before
