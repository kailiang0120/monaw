"""Native filesystem integration regressions for durable approval and grants."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.agent import access_grant_broker, approval_broker
from app.agent.access_grant_broker import resolve_grant
from app.agent.approval_broker import approve_ticket
from app.agent.database import Database
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
from app.skills.filesystem import file_ops


@pytest.fixture
def native_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import app.agent.controller_policy as controller_policy

    root = tmp_path / "workspace"
    root.mkdir()
    policy_dir = tmp_path / "policy"
    policy_dir.mkdir()
    monkeypatch.setattr(controller_policy, "_POLICY_DIR", policy_dir)
    monkeypatch.setattr(controller_policy, "_POLICY_FILE", policy_dir / "controller_policy.md")
    monkeypatch.setattr(controller_policy, "_ALLOWLIST_FILE", policy_dir / "allowlisted_apps.md")

    db = Database(tmp_path / "agent.db")
    db.init_db()
    monkeypatch.setattr(access_grant_broker, "get_db", lambda: db)
    chat_id = "chat-native-file-integration"
    db.create_conversation(chat_id, "Native file integration", str(root))

    approval_dir = tmp_path / "approvals"
    monkeypatch.setattr(approval_broker, "_APPROVALS_DIR", approval_dir)
    monkeypatch.setattr(approval_broker, "_TICKETS_FILE", approval_dir / "tickets.jsonl")
    monkeypatch.setattr(approval_broker, "_APPROVAL_LOG", tmp_path / "approval_log.md")
    approval_broker._pending_index.clear()
    approval_broker._all_tickets.clear()
    access_grant_broker._pending.clear()
    access_grant_broker._all.clear()
    access_grant_broker._resume_events.clear()
    access_grant_broker._resume_decisions.clear()

    snapshot_store = SnapshotStore(tmp_path / "snapshots")
    monkeypatch.setattr(file_ops, "_p3_snapshot_store", lambda: snapshot_store)

    session_id = "session-native-file-integration"
    principal_id = "principal-native-file-integration"
    conversation_token = set_current_conversation_id(chat_id)
    session_token = set_current_control_session_id(session_id)
    principal_token = set_current_principal_id(principal_id)

    def set_policy(*, mode: str, rule: PathRule | None = None) -> None:
        settings = AgentSettings()
        settings.permissions.mode = mode
        settings.permissions.blocked_roots = []
        settings.permissions.path_rules = [rule] if rule else []
        settings.permissions.allow_delete = True
        settings.permissions.confirmations.mutate = True
        save_agent_settings(settings, settings_path=policy_dir.parent / "settings.json")

    run = create_durable_run(
        conversation_id=chat_id,
        execution_source="desktop",
        principal_id=principal_id,
        control_session_id=session_id,
        workspace_dir=str(root),
        run_id="run-native-file-integration",
        turn_id="turn-native-file-integration",
        db=db,
    )

    try:
        yield {
            "db": db,
            "root": root,
            "run": run,
            "session_id": session_id,
            "set_policy": set_policy,
            "snapshot_store": snapshot_store,
        }
    finally:
        reset_current_principal_id(principal_token)
        reset_current_control_session_id(session_token)
        reset_current_conversation_id(conversation_token)
        approval_broker._pending_index.clear()
        approval_broker._all_tickets.clear()
        access_grant_broker._pending.clear()
        access_grant_broker._all.clear()
        access_grant_broker._resume_events.clear()
        access_grant_broker._resume_decisions.clear()
        db.close()


def _invoke_file_write(operation, *, path: Path, content: str, overwrite: bool, bypass: bool = False):
    operation.begin_effect()
    token = set_current_operation_execution(operation)
    try:
        output = file_ops.file_write(
            str(path), content, overwrite=overwrite, _bypass_gate=bypass
        )
    finally:
        reset_current_operation_execution(token)
    operation.note_pending_preflight(output)
    return json.loads(output)


def _start_operation(run):
    return prepare_tool_operation(
        run,
        tool_name="fs_write",
        trusted_builtin=True,
    )


def test_stale_approved_native_write_returns_fresh_review_without_overwriting(
    native_context,
):
    root = native_context["root"]
    native_context["set_policy"](
        mode="default",
        rule=PathRule(path=str(root), read=True, write=True, delete=True, launch=False),
    )
    target = root / "notes.txt"
    target.write_text("original", encoding="utf-8")
    operation = _start_operation(native_context["run"])

    pending = _invoke_file_write(
        operation, path=target, content="approved text", overwrite=True
    )
    assert pending["status"] == "pending_approval"
    ticket = approval_broker.get_ticket(pending["ticket_id"])
    assert ticket is not None
    assert "original" not in repr(ticket.model_dump())
    approve_ticket(ticket.id)

    target.write_text("external edit", encoding="utf-8")
    replay = _invoke_file_write(
        operation, path=target, content="approved text", overwrite=True, bypass=True
    )

    assert replay["status"] == "error"
    assert replay["reason_code"] == "stale_preview"
    assert target.read_text(encoding="utf-8") == "external edit"
    operation_row = native_context["run"].journal.load_operation(
        native_context["run"].run_id, operation.operation_id
    )
    assert operation_row["status"] == "failed"
    assert operation_row.get("snapshot_ref") is None


def test_once_grant_then_mutation_approval_replays_legacy_write_once(native_context):
    root = native_context["root"]
    native_context["set_policy"](mode="custom")
    target = root / "once.txt"
    target.write_text("original", encoding="utf-8")
    operation = _start_operation(native_context["run"])

    access_pending = _invoke_file_write(
        operation, path=target, content="authorized once", overwrite=True
    )
    assert access_pending["status"] == "pending_access_grant"
    resolved = resolve_grant(
        access_pending["ticket_id"],
        "once",
        expected_session_id=native_context["session_id"],
    )
    assert resolved is not None and resolved.status == "granted"

    approval_pending = _invoke_file_write(
        operation, path=target, content="authorized once", overwrite=True
    )
    assert approval_pending["status"] == "pending_approval"
    assert "preview" not in approval_pending
    assert access_grant_broker.session_grant_kind("path", str(target), "write") is None
    approval = approval_broker.get_ticket(approval_pending["ticket_id"])
    assert approval is not None
    approve_ticket(approval.id)

    completed = _invoke_file_write(
        operation,
        path=target,
        content="authorized once",
        overwrite=True,
        bypass=True,
    )
    operation.settle(json.dumps(completed))

    assert completed["status"] == "ok"
    assert target.read_text(encoding="utf-8") == "authorized once"
    assert list(native_context["snapshot_store"].root.glob("*.snap")) == []
    operation_row = native_context["run"].journal.load_operation(
        native_context["run"].run_id, operation.operation_id
    )
    assert operation_row["status"] == "returned"
    assert operation_row["effect_class"] == "opaque_effect"


@pytest.mark.parametrize("operation", ["append", "patch"])
def test_append_and_patch_dry_run_include_authorized_unified_diff(
    native_context, monkeypatch, operation
):
    root = native_context["root"]
    native_context["set_policy"](
        mode="full_access",
        rule=PathRule(path=str(root), read=True, write=True, delete=True, launch=False),
    )
    target = root / f"{operation}.txt"
    original = b"before line\n"
    target.write_bytes(original)
    monkeypatch.setattr(
        SnapshotStore,
        "capture",
        lambda *_args, **_kwargs: pytest.fail("Dry Run must not snapshot"),
    )

    if operation == "append":
        result = json.loads(file_ops.file_append(str(target), "after line\n", dry_run=True))
    else:
        result = json.loads(
            file_ops.file_patch(str(target), "before", "after", dry_run=True)
        )

    preview = result["preview"]["preview"]
    assert result["status"] == "ok"
    assert result["dry_run"] is True
    assert preview["kind"] == "unified_diff"
    assert (
        " before line" in preview["text"]
        if operation == "append"
        else "-before line" in preview["text"]
    )
    assert "+after line" in preview["text"]
    assert target.read_bytes() == original


@pytest.mark.parametrize("operation", ["append", "patch"])
def test_append_and_patch_dry_run_never_read_without_read_permission(
    native_context, monkeypatch, operation
):
    root = native_context["root"]
    native_context["set_policy"](
        mode="full_access",
        rule=PathRule(path=str(root), read=False, write=True, delete=True, launch=False),
    )
    target = root / f"private-{operation}.txt"
    original = b"private original line\n"
    target.write_bytes(original)
    target_key = os.path.normcase(os.path.abspath(target))
    original_path_open = Path.open
    original_os_open = os.open

    def is_target(path) -> bool:
        try:
            return os.path.normcase(os.path.abspath(os.fspath(path))) == target_key
        except (TypeError, ValueError, OSError):
            return False

    def guarded_path_open(path, mode="r", *args, **kwargs):
        if is_target(path):
            pytest.fail("Dry Run read target contents without READ permission")
        return original_path_open(path, mode, *args, **kwargs)

    def guarded_os_open(path, flags, *args, **kwargs):
        if is_target(path) and (flags & os.O_ACCMODE) == os.O_RDONLY:
            pytest.fail("Dry Run fingerprinted target contents without READ permission")
        return original_os_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as deny_reads:
        deny_reads.setattr(Path, "open", guarded_path_open)
        deny_reads.setattr(os, "open", guarded_os_open)
        if operation == "append":
            raw = file_ops.file_append(str(target), "replacement text\n", dry_run=True)
        else:
            raw = file_ops.file_patch(
                str(target), "private", "replacement", dry_run=True
            )
    result = json.loads(raw)

    assert result["status"] == "ok"
    assert result["dry_run"] is True
    assert result["preview"]["preview"] == {
        "kind": "summary",
        "reason": "read_permission_required",
    }
    assert "private original line" not in repr(result)
    assert "replacement" not in repr(result)
    assert target.read_bytes() == original
