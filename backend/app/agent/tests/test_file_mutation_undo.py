"""Acceptance tests for snapshot-backed native file Undo."""

from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.agent import access_grant_broker, approval_broker
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
def undo_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
    chat_id = "chat-file-undo-acceptance"
    session_id = "session-file-undo-acceptance"
    principal_id = session_id
    db.create_conversation(chat_id, "File Undo acceptance", str(root))

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

    store = SnapshotStore(tmp_path / "snapshots")
    monkeypatch.setattr(file_ops, "_p3_snapshot_store", lambda: store)
    conversation_token = set_current_conversation_id(chat_id)
    session_token = set_current_control_session_id(session_id)
    principal_token = set_current_principal_id(principal_id)

    def set_policy(*, read: bool = True, write: bool = True, delete: bool = True) -> None:
        settings = AgentSettings()
        settings.permissions.mode = "full_access"
        settings.permissions.blocked_roots = []
        settings.permissions.path_rules = [
            PathRule(
                path=str(root),
                read=read,
                write=write,
                delete=delete,
                launch=False,
            )
        ]
        settings.permissions.allow_delete = True
        settings.permissions.dangerous_actions_require_confirm = False
        settings.permissions.confirmations.mutate = False
        settings.permissions.confirmations.delete = False
        save_agent_settings(settings, settings_path=policy_dir.parent / "settings.json")

    set_policy()
    run = create_durable_run(
        conversation_id=chat_id,
        execution_source="desktop",
        principal_id=principal_id,
        control_session_id=session_id,
        workspace_dir=str(root),
        run_id="run-file-undo-acceptance",
        turn_id="turn-file-undo-acceptance",
        db=db,
    )

    try:
        yield {
            "db": db,
            "root": root,
            "run": run,
            "store": store,
            "session_id": session_id,
            "principal_id": principal_id,
            "set_policy": set_policy,
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


def _perform_mutation(
    context,
    kind: str,
    target: Path,
    *,
    overwrite_move: bool = False,
) -> tuple[str, dict[str, object]]:
    run = context["run"]
    tool_name = {"write": "fs_write", "move": "fs_move", "delete": "fs_delete"}[kind]
    operation = prepare_tool_operation(run, tool_name=tool_name, trusted_builtin=True)
    assert operation is not None
    operation.begin_effect()
    token = set_current_operation_execution(operation)
    try:
        if kind == "write":
            raw = file_ops.file_write(str(target), "after image\n", overwrite=True)
        elif kind == "move":
            destination = target.with_name("moved.txt")
            if overwrite_move:
                destination.write_bytes(b"old destination\n")
            raw = file_ops.fs_move(
                str(target), str(destination), overwrite=overwrite_move
            )
        else:
            raw = file_ops.fs_delete(str(target), recursive=False)
    finally:
        reset_current_operation_execution(token)
    operation.note_pending_preflight(raw)
    operation.settle(raw)
    result = json.loads(raw)
    assert result["status"] == "ok", result
    row = run.journal.load_operation_internal(run.run_id, operation.operation_id)
    assert row is not None
    assert row["effect_class"] == "verifiable_write"
    assert row["status"] == "verified"
    return operation.operation_id, result


def _undo(context, operation_id: str, **kwargs):
    from app.agent.file_mutation.undo_manager import UndoManager

    _finish_run(context)
    kwargs.setdefault("principal_id", context["principal_id"])
    kwargs.setdefault("idempotency_key", f"test-undo-{uuid.uuid4().hex}")
    return UndoManager(
        context["run"].journal,
        store=context["store"],
        permission_check=lambda _action, _path: True,
    ).undo(context["run"].run_id, operation_id, **kwargs)


def _finish_run(context) -> None:
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


class _SimulatedUndoProcessCrash(BaseException):
    """Bypass ordinary Undo error handling while preserving journaled state."""


def _crash_undo_after_effect(
    context,
    operation_id: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    idempotency_key: str,
    crash_after_steps: int = 1,
) -> tuple[str, str, list[dict[str, object]], list[int]]:
    from app.agent.file_mutation.undo_manager import UndoManager

    _finish_run(context)
    old_owner = f"owner-v1-{uuid.uuid4().hex}"
    manager = UndoManager(
        context["run"].journal,
        store=context["store"],
        permission_check=lambda _action, _path: True,
        owner_provider=lambda: old_owner,
    )
    original_step = UndoManager._apply_inverse_step
    calls: list[int] = []

    def apply_then_crash(self, *args, **kwargs):
        result = original_step(self, *args, **kwargs)
        calls.append(len(calls) + 1)
        if len(calls) == crash_after_steps:
            raise _SimulatedUndoProcessCrash("simulated process termination after file effect")
        return result

    monkeypatch.setattr(UndoManager, "_apply_inverse_step", apply_then_crash)
    with pytest.raises(_SimulatedUndoProcessCrash, match="simulated process termination"):
        manager.undo(
            context["run"].run_id,
            operation_id,
            principal_id=context["principal_id"],
            idempotency_key=idempotency_key,
        )

    journal = context["run"].journal
    parent_rows = [
        row for row in journal.list_operations(context["run"].run_id)
        if row["operation_kind"] == "undo"
    ]
    assert len(parent_rows) == 1
    parent_id = str(parent_rows[0]["operation_id"])
    steps = journal.load_undo_steps(context["run"].run_id, parent_id)
    assert len(calls) == crash_after_steps
    return old_owner, parent_id, steps, calls


def _recover_pending_undo(
    context,
    monkeypatch: pytest.MonkeyPatch,
    *,
    old_owner: str,
    use_startup: bool,
):
    from app.agent.durable.recovery import recover_pending_undo_operations, recover_startup_runs
    from app.agent.file_mutation.verifier import FileMutationVerifier

    journal = context["run"].journal
    recovery_owner = f"owner-v1-{uuid.uuid4().hex}"
    owner_proof = lambda owner: "dead" if owner == old_owner else "unknown"
    verifier = FileMutationVerifier(
        context["store"], read_policy=lambda _path: True
    ).verify
    if use_startup:
        # Startup cleanup must use this test's temporary snapshot directory.
        import app.agent.file_mutation.snapshot_store as snapshot_store_module

        monkeypatch.setattr(
            snapshot_store_module, "SnapshotStore", lambda *args, **kwargs: context["store"]
        )
        report = recover_startup_runs(
            journal=journal,
            recovery_owner_instance_id=recovery_owner,
            owner_proof=owner_proof,
            read_only_verifier=verifier,
        )
    else:
        report = recover_pending_undo_operations(
            journal=journal,
            recovery_owner_instance_id=recovery_owner,
            owner_proof=owner_proof,
            verifier=verifier,
        )
    return report, recovery_owner


@pytest.mark.parametrize("kind", ["write", "move", "delete"])
def test_verified_write_move_and_delete_can_be_undone(undo_context, kind: str):
    root = undo_context["root"]
    target = root / "target.txt"
    before = b"before image\n"
    target.write_bytes(before)

    operation_id, _ = _perform_mutation(undo_context, kind, target)
    if kind == "write":
        assert target.read_bytes() == b"after image\n"
    elif kind == "move":
        assert not target.exists()
        assert target.with_name("moved.txt").read_bytes() == before
    else:
        assert not target.exists()

    result = _undo(undo_context, operation_id)

    assert result["status"] == "undone"
    assert result["operation_id"] == operation_id
    assert result["undo_operation_id"]
    if kind == "move":
        assert target.read_bytes() == before
        assert not target.with_name("moved.txt").exists()
    else:
        assert target.read_bytes() == before


def test_undo_of_a_new_file_removes_it(undo_context):
    target = undo_context["root"] / "created.txt"
    operation = prepare_tool_operation(
        undo_context["run"], tool_name="fs_write", trusted_builtin=True
    )
    assert operation is not None
    operation.begin_effect()
    token = set_current_operation_execution(operation)
    try:
        raw = file_ops.file_write(str(target), "created by Monaw\n", overwrite=True)
    finally:
        reset_current_operation_execution(token)
    operation.settle(raw)
    result = json.loads(raw)
    assert result["status"] == "ok", result
    assert target.exists()

    undone = _undo(undo_context, operation.operation_id)

    assert undone["status"] == "undone"
    assert not target.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows handle-bound delete")
def test_recovery_cleanup_preserves_path_replacement_after_fingerprint(
    undo_context, monkeypatch: pytest.MonkeyPatch
):
    from app.agent.file_mutation.fingerprint import fingerprint_path
    from app.agent.file_mutation.planner import PathState, PlanOperand, build_plan
    from app.agent.file_mutation.undo_manager import UndoManager
    from app.skills.filesystem import file_ops

    target = undo_context["root"] / "cleanup-race.txt"
    original_bytes = b"artifact owned by this Undo\n"
    replacement_bytes = b"user replacement must survive\n"
    target.write_bytes(original_bytes)
    before_fp = fingerprint_path(target, include_bytes=True)
    before = PathState.from_fingerprint(before_fp)
    after = PathState.metadata(
        target,
        exists=False,
        kind="absent",
        size=0,
        parent_identity=before.parent_identity,
    )
    plan = build_plan(
        "delete_file",
        {"recovery_cleanup_race": True},
        [PlanOperand(str(target), "target", before, after, "absent")],
        before_bytes=original_bytes,
        read_authorized=True,
    )
    operation_id = "undo-step-cleanup-race"
    snapshot_ref = undo_context["store"].capture(
        plan,
        {str(target): original_bytes},
        run_id=undo_context["run"].run_id,
        operation_id=operation_id,
        authorized_roots=[str(undo_context["root"])],
    )
    artifact = target.parent / (
        f".monaw-undo-remove-{hashlib.sha256(operation_id.encode()).hexdigest()[:24]}.tmp"
    )
    target.replace(artifact)

    p3_delete = file_ops._p3_delete_regular_file

    def replace_then_delete(path, expected):
        path.unlink()
        path.write_bytes(replacement_bytes)
        return p3_delete(path, expected)

    monkeypatch.setattr(file_ops, "_p3_delete_regular_file", replace_then_delete)
    removed = UndoManager.cleanup_recovered_artifact(
        {
            "run_id": undo_context["run"].run_id,
            "operation_id": operation_id,
            "operation_kind": "file_delete",
            "snapshot_ref": snapshot_ref,
        },
        store=undo_context["store"],
        permission_check=lambda _action, _path: True,
    )

    assert removed is False
    assert artifact.read_bytes() == replacement_bytes
    assert not target.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows handle-bound delete")
def test_stage_cleanup_preserves_path_replacement_after_fingerprint(
    undo_context, monkeypatch: pytest.MonkeyPatch
):
    from app.agent.file_mutation.fingerprint import fingerprint_path
    from app.agent.file_mutation.planner import PathState, PlanOperand, build_plan
    from app.agent.file_mutation.undo_manager import UndoManager, _absent_state
    from app.skills.filesystem import file_ops

    target = undo_context["root"] / "stage-cleanup-race-target.txt"
    stage = undo_context["root"] / ".monaw-undo-stage-cleanup-race.tmp"
    original_bytes = b"staged bytes owned by this Undo\n"
    replacement_bytes = b"stage path replacement must survive\n"
    stage.write_bytes(original_bytes)
    stage_fp = fingerprint_path(stage, include_bytes=True)
    before = _absent_state(str(target), stage_fp.parent_identity)
    after = PathState.from_bytes(
        target,
        original_bytes,
        identity=stage_fp.identity,
        modified_ns=stage_fp.modified_ns,
        created_ns=stage_fp.created_ns,
        parent_identity=stage_fp.parent_identity,
    )
    plan = build_plan(
        "write_file",
        {"stage_cleanup_race": True},
        [PlanOperand(str(target), "target", before, after, "replaced")],
        after_bytes=original_bytes,
        read_authorized=True,
    )
    p3_delete = file_ops._p3_delete_regular_file

    def replace_then_delete(path, expected):
        path.unlink()
        path.write_bytes(replacement_bytes)
        return p3_delete(path, expected)

    monkeypatch.setattr(file_ops, "_p3_delete_regular_file", replace_then_delete)
    removed = UndoManager._cleanup_stage(stage, plan)

    assert removed is False
    assert stage.read_bytes() == replacement_bytes


@pytest.mark.parametrize("change", ["edit", "delete"])
def test_external_edit_or_delete_after_write_is_preserved_and_reports_conflict(
    undo_context, change: str
):
    from app.agent.file_mutation.undo_manager import UndoConflictError

    target = undo_context["root"] / "conflict.txt"
    target.write_bytes(b"before\n")
    operation_id, _ = _perform_mutation(undo_context, "write", target)
    if change == "edit":
        target.write_bytes(b"user edit C\n")
    else:
        target.unlink()

    with pytest.raises(UndoConflictError) as raised:
        _undo(undo_context, operation_id)

    assert getattr(raised.value, "reason_code", "") == "undo_conflict"
    if change == "edit":
        assert target.read_bytes() == b"user edit C\n"
    else:
        assert not target.exists()


def test_move_undo_refuses_to_overwrite_a_source_that_was_reoccupied(undo_context):
    from app.agent.file_mutation.undo_manager import UndoConflictError

    source = undo_context["root"] / "source.txt"
    original = b"move me\n"
    source.write_bytes(original)
    operation_id, _ = _perform_mutation(undo_context, "move", source)
    source.write_bytes(b"new occupant\n")
    destination = source.with_name("moved.txt")

    with pytest.raises(UndoConflictError) as raised:
        _undo(undo_context, operation_id)

    assert getattr(raised.value, "reason_code", "") == "undo_conflict"
    assert source.read_bytes() == b"new occupant\n"
    assert destination.read_bytes() == original


def test_undo_rechecks_read_and_inverse_mutation_permissions(undo_context):
    from app.agent.file_mutation.undo_manager import UndoPermissionError
    from app.agent.permission_actions import ActionType

    target = undo_context["root"] / "permission.txt"
    target.write_bytes(b"before\n")
    operation_id, _ = _perform_mutation(undo_context, "write", target)
    from app.agent.file_mutation.undo_manager import UndoManager
    _finish_run(undo_context)
    for denied_action in (ActionType.READ, ActionType.MUTATE):
        checked: list[tuple[object, str]] = []

        def permission_check(action, path):
            checked.append((action, path))
            return action != denied_action

        manager = UndoManager(
            undo_context["run"].journal,
            store=undo_context["store"],
            permission_check=permission_check,
        )
        with pytest.raises(UndoPermissionError):
            manager.undo(
                undo_context["run"].run_id,
                operation_id,
                principal_id=undo_context["principal_id"],
                idempotency_key=f"permission-denied-{denied_action.value}",
            )

        assert checked
        assert denied_action in {action for action, _path in checked}
        assert all(str(target) == path for _action, path in checked)
        assert target.read_bytes() == b"after image\n"


def test_concurrent_undo_claims_create_only_one_inverse(undo_context):
    target = undo_context["root"] / "concurrent.txt"
    target.write_bytes(b"before\n")
    operation_id, _ = _perform_mutation(undo_context, "write", target)
    _finish_run(undo_context)
    barrier = threading.Barrier(3)

    def call_undo():
        barrier.wait(timeout=5)
        try:
            return _undo(undo_context, operation_id)
        except Exception as exc:  # The loser may receive a typed in-progress/conflict result.
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(call_undo)
        second = pool.submit(call_undo)
        barrier.wait(timeout=5)
        outcomes = [first.result(timeout=10), second.result(timeout=10)]

    successful = [
        value for value in outcomes
        if isinstance(value, dict) and value.get("status") == "undone"
    ]
    assert successful
    assert len({value["undo_operation_id"] for value in successful}) == 1
    assert target.read_bytes() == b"before\n"
    undo_rows = [
        row for row in undo_context["run"].journal.list_operations(undo_context["run"].run_id)
        if row["operation_kind"] == "undo"
    ]
    assert len(undo_rows) == 1


def test_undo_retry_with_same_idempotency_key_does_not_apply_twice(undo_context):
    target = undo_context["root"] / "idempotent.txt"
    target.write_bytes(b"before\n")
    operation_id, _ = _perform_mutation(undo_context, "write", target)
    _finish_run(undo_context)
    key = "undo-idempotency-key-1"
    expected_revision = undo_context["run"].journal.snapshot(
        undo_context["run"].run_id
    )["revision"]

    first = _undo(
        undo_context,
        operation_id,
        expected_revision=expected_revision,
        idempotency_key=key,
    )
    retry = _undo(
        undo_context,
        operation_id,
        expected_revision=expected_revision,
        idempotency_key=key,
    )

    assert first["status"] == retry["status"] == "undone"
    assert first["undo_operation_id"] == retry["undo_operation_id"]
    assert target.read_bytes() == b"before\n"
    undo_rows = [
        row for row in undo_context["run"].journal.list_operations(undo_context["run"].run_id)
        if row["operation_kind"] == "undo"
    ]
    assert len(undo_rows) == 1


def test_expired_snapshot_is_unavailable_without_touching_the_file(undo_context, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.agent.file_mutation.undo_manager import UndoUnavailableError

    target = undo_context["root"] / "expired.txt"
    target.write_bytes(b"before\n")
    operation_id, _ = _perform_mutation(undo_context, "write", target)
    original_load = undo_context["store"].load

    def expired_load(*args, **kwargs):
        snapshot = original_load(*args, **kwargs)
        snapshot["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        return snapshot

    monkeypatch.setattr(undo_context["store"], "load", expired_load)

    with pytest.raises(UndoUnavailableError):
        _undo(undo_context, operation_id)

    assert target.read_bytes() == b"after image\n"


def test_inverse_failure_is_persisted_as_needs_review_and_restart_does_not_replay(
    undo_context, monkeypatch: pytest.MonkeyPatch
):
    from app.agent.file_mutation.undo_manager import UndoManager

    target = undo_context["root"] / "crash-window.txt"
    target.write_bytes(b"before\n")
    operation_id, _ = _perform_mutation(undo_context, "write", target)
    manager = UndoManager(
        undo_context["run"].journal,
        store=undo_context["store"],
        permission_check=lambda _action, _path: True,
    )
    _finish_run(undo_context)
    original_step = UndoManager._apply_inverse_step
    calls = 0
    idempotency_key = "crash-window-undo"

    class SimulatedCrash(BaseException):
        pass

    def apply_then_crash(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        result = original_step(self, *args, **kwargs)
        raise SimulatedCrash("simulated process crash after inverse filesystem effect")

    monkeypatch.setattr(UndoManager, "_apply_inverse_step", apply_then_crash)
    with pytest.raises(SimulatedCrash, match="simulated process crash"):
        manager.undo(
            undo_context["run"].run_id,
            operation_id,
            principal_id=undo_context["principal_id"],
            idempotency_key=idempotency_key,
        )
    after_first = target.read_bytes()

    restarted = UndoManager(
        undo_context["run"].journal,
        store=undo_context["store"],
        permission_check=lambda _action, _path: True,
    )
    status = restarted.undo(
        undo_context["run"].run_id,
        operation_id,
        principal_id=undo_context["principal_id"],
        idempotency_key=idempotency_key,
    )

    assert calls == 1
    assert target.read_bytes() == after_first
    assert status["status"] in {"needs_review", "in_progress"}


def test_undo_partial_failure_preserves_completed_steps_for_review(undo_context, monkeypatch):
    """A move inverse has two path effects and must not claim all-or-nothing success."""
    from app.agent.file_mutation.undo_manager import UndoManager

    source = undo_context["root"] / "partial-source.txt"
    destination = source.with_name("moved.txt")
    original = b"partial move before image\n"
    source.write_bytes(original)
    operation_id, _ = _perform_mutation(
        undo_context, "move", source, overwrite_move=True
    )
    manager = UndoManager(
        undo_context["run"].journal,
        store=undo_context["store"],
        permission_check=lambda _action, _path: True,
    )
    _finish_run(undo_context)
    original_step = UndoManager._apply_inverse_step
    calls = 0
    idempotency_key = "partial-move-undo"

    class SimulatedPartialFailure(BaseException):
        pass

    def fail_after_first_step(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        result = original_step(self, *args, **kwargs)
        if calls == 1:
            raise SimulatedPartialFailure("injected partial inverse failure")
        return result

    monkeypatch.setattr(UndoManager, "_apply_inverse_step", fail_after_first_step)
    with pytest.raises(SimulatedPartialFailure, match="injected partial inverse failure"):
        manager.undo(
            undo_context["run"].run_id,
            operation_id,
            principal_id=undo_context["principal_id"],
            idempotency_key=idempotency_key,
        )
    state_before_retry = (
        source.read_bytes() if source.exists() else None,
        destination.read_bytes() if destination.exists() else None,
    )

    assert calls == 1
    # The manager records a reviewable partial outcome; later requests must not
    # blindly replay the inverse against whichever path remains.
    restarted = UndoManager(
        undo_context["run"].journal,
        store=undo_context["store"],
        permission_check=lambda _action, _path: True,
    )
    outcome = restarted.undo(
        undo_context["run"].run_id,
        operation_id,
        principal_id=undo_context["principal_id"],
        idempotency_key=idempotency_key,
    )
    assert outcome["status"] in {"needs_review", "in_progress"}
    assert calls == 1
    assert (
        source.read_bytes() if source.exists() else None,
        destination.read_bytes() if destination.exists() else None,
    ) == state_before_retry


def test_startup_recovery_verifies_a_completed_inverse_without_replaying_it(
    undo_context, monkeypatch: pytest.MonkeyPatch
):
    target = undo_context["root"] / "startup-recovered.txt"
    target.write_bytes(b"before startup recovery\n")
    operation_id, _ = _perform_mutation(undo_context, "write", target)
    old_owner, undo_id, steps, calls = _crash_undo_after_effect(
        undo_context,
        operation_id,
        monkeypatch,
        idempotency_key="startup-completed-inverse",
    )

    assert target.read_bytes() == b"before startup recovery\n"
    assert len(steps) == 1 and steps[0]["status"] == "executing"
    assert undo_context["run"].journal.load_operation_internal(
        undo_context["run"].run_id, undo_id
    )["status"] == "executing"

    report, _recovery_owner = _recover_pending_undo(
        undo_context, monkeypatch, old_owner=old_owner, use_startup=True
    )

    parent = undo_context["run"].journal.load_operation_internal(
        undo_context["run"].run_id, undo_id
    )
    recovered_steps = undo_context["run"].journal.load_undo_steps(
        undo_context["run"].run_id, undo_id
    )
    assert report["undo_scanned"] == report["undo_recovered"] == 1
    assert report["undo_verified"] == 2  # one step and its parent
    assert report["undo_needs_review"] == report["undo_errors"] == 0
    assert parent["status"] == "verified"
    assert recovered_steps[0]["status"] == "verified"
    assert target.read_bytes() == b"before startup recovery\n"
    assert calls == [1]

    second_report, _ = _recover_pending_undo(
        undo_context, monkeypatch, old_owner=old_owner, use_startup=True
    )
    assert second_report["undo_scanned"] == 0
    assert target.read_bytes() == b"before startup recovery\n"
    assert calls == [1]


def test_pending_undo_recovery_marks_unknown_inverse_needs_review_without_replay(
    undo_context, monkeypatch: pytest.MonkeyPatch
):
    target = undo_context["root"] / "unknown-inverse.txt"
    target.write_bytes(b"before unknown recovery\n")
    operation_id, _ = _perform_mutation(undo_context, "write", target)
    old_owner, undo_id, _steps, calls = _crash_undo_after_effect(
        undo_context,
        operation_id,
        monkeypatch,
        idempotency_key="unknown-inverse-state",
    )
    # This state matches neither side of the inverse's durable file snapshot.
    target.write_bytes(b"third party state C\n")

    report, recovery_owner = _recover_pending_undo(
        undo_context, monkeypatch, old_owner=old_owner, use_startup=False
    )

    journal = undo_context["run"].journal
    parent = journal.load_operation_internal(undo_context["run"].run_id, undo_id)
    steps = journal.load_undo_steps(undo_context["run"].run_id, undo_id)
    assert report["undo_scanned"] == report["undo_recovered"] == 1
    assert report["undo_needs_review"] == 1
    assert report["undo_verified"] == report["undo_errors"] == 0
    assert parent["status"] == "needs_review"
    assert steps[0]["status"] == "needs_review"
    assert target.read_bytes() == b"third party state C\n"
    assert calls == [1]

    # A later startup under another owner may inspect again, but it still must
    # leave the uncertain file as found and must not invoke an inverse effect.
    from app.agent.durable.recovery import recover_pending_undo_operations

    followup_report = recover_pending_undo_operations(
        journal=journal,
        recovery_owner_instance_id=f"owner-v1-{uuid.uuid4().hex}",
        owner_proof=lambda owner: "dead" if owner == recovery_owner else "unknown",
        verifier=lambda _operation: {"verification_result": "unknown"},
    )
    assert followup_report["undo_scanned"] == 1
    assert followup_report["undo_recovered"] == 1
    assert target.read_bytes() == b"third party state C\n"
    assert calls == [1]


def test_recovery_of_first_overwritten_move_inverse_step_does_not_start_second(
    undo_context, monkeypatch: pytest.MonkeyPatch
):
    source = undo_context["root"] / "recovery-partial-source.txt"
    destination = source.with_name("moved.txt")
    original_source = b"source before overwrite move\n"
    original_destination = b"destination before overwrite move\n"
    source.write_bytes(original_source)
    operation_id, _ = _perform_mutation(
        undo_context, "move", source, overwrite_move=True
    )
    old_owner, undo_id, steps, calls = _crash_undo_after_effect(
        undo_context,
        operation_id,
        monkeypatch,
        idempotency_key="partial-overwrite-move-recovery",
    )

    assert len(steps) == 1
    assert steps[0]["status"] == "executing"
    assert source.read_bytes() == original_source
    assert not destination.exists()

    report, recovery_owner = _recover_pending_undo(
        undo_context, monkeypatch, old_owner=old_owner, use_startup=True
    )

    journal = undo_context["run"].journal
    parent = journal.load_operation_internal(undo_context["run"].run_id, undo_id)
    recovered_steps = journal.load_undo_steps(undo_context["run"].run_id, undo_id)
    assert report["undo_scanned"] == report["undo_recovered"] == 1
    assert report["undo_verified"] == 1
    assert report["undo_needs_review"] == 1
    assert report["undo_errors"] == 0
    assert parent["status"] == "needs_review"
    assert len(recovered_steps) == 1
    assert recovered_steps[0]["status"] == "verified"
    assert source.read_bytes() == original_source
    assert not destination.exists()
    assert calls == [1]

    from app.agent.durable.recovery import recover_startup_runs

    followup_report = recover_startup_runs(
        journal=journal,
        recovery_owner_instance_id=f"owner-v1-{uuid.uuid4().hex}",
        owner_proof=lambda owner: "dead" if owner == recovery_owner else "unknown",
        read_only_verifier=lambda _operation: {"verification_result": "completed"},
    )
    assert followup_report["undo_scanned"] == 1
    assert followup_report["undo_recovered"] == 1
    assert len(journal.load_undo_steps(undo_context["run"].run_id, undo_id)) == 1
    assert source.read_bytes() == original_source
    assert not destination.exists()
    assert calls == [1]


def test_child_process_exit_after_inverse_effect_is_recovered_from_disk(
    undo_context, monkeypatch: pytest.MonkeyPatch
):
    """Exercise an actual abrupt process exit after the real inverse effect."""
    from app.agent.durable.recovery import recover_startup_runs
    from app.agent.file_mutation.verifier import FileMutationVerifier

    target = undo_context["root"] / "subprocess-crash.txt"
    target.write_bytes(b"before child process\n")
    operation_id, _ = _perform_mutation(undo_context, "write", target)
    _finish_run(undo_context)
    owner_id = f"owner-v1-{uuid.uuid4().hex}"
    idempotency_key = "subprocess-interrupted-undo"
    child_script = r'''
import os
import sys
from pathlib import Path
from app.agent.database import Database
from app.agent.durable.journal import Journal
from app.agent.file_mutation.snapshot_store import SnapshotStore
from app.agent.file_mutation.undo_manager import UndoManager

db = Database(Path(sys.argv[1]))
db.init_db()
journal = Journal(db)
manager = UndoManager(
    journal,
    store=SnapshotStore(Path(sys.argv[2])),
    permission_check=lambda _action, _path: True,
    owner_provider=lambda: sys.argv[6],
)
original = UndoManager._apply_inverse_step
def exit_after_real_effect(self, *args, **kwargs):
    original(self, *args, **kwargs)
    os._exit(87)
UndoManager._apply_inverse_step = exit_after_real_effect
manager.undo(
    sys.argv[3], sys.argv[4], principal_id=sys.argv[5], idempotency_key=sys.argv[7]
)
os._exit(88)
'''
    backend_dir = Path(__file__).resolve().parents[3]
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            child_script,
            str(undo_context["db"]._path),
            str(undo_context["store"].root),
            undo_context["run"].run_id,
            operation_id,
            undo_context["principal_id"],
            owner_id,
            idempotency_key,
        ],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert child.returncode == 87, child.stderr
    assert target.read_bytes() == b"before child process\n"

    journal = undo_context["run"].journal
    pending = journal.list_pending_undo_operations()
    assert len(pending) == 1
    undo_id = str(pending[0]["operation_id"])
    assert journal.load_undo_steps(undo_context["run"].run_id, undo_id)[0]["status"] == "executing"

    import app.agent.file_mutation.snapshot_store as snapshot_store_module

    monkeypatch.setattr(
        snapshot_store_module, "SnapshotStore", lambda *args, **kwargs: undo_context["store"]
    )
    report = recover_startup_runs(
        journal=journal,
        recovery_owner_instance_id=f"owner-v1-{uuid.uuid4().hex}",
        owner_proof=lambda owner: "dead" if owner == owner_id else "unknown",
        read_only_verifier=FileMutationVerifier(
            undo_context["store"], read_policy=lambda _path: True
        ).verify,
    )

    parent = journal.load_operation_internal(undo_context["run"].run_id, undo_id)
    step = journal.load_undo_steps(undo_context["run"].run_id, undo_id)[0]
    assert report["undo_recovered"] == 1
    assert report["undo_verified"] == 2
    assert report["undo_needs_review"] == report["undo_errors"] == 0
    assert parent["status"] == step["status"] == "verified"
    assert target.read_bytes() == b"before child process\n"

