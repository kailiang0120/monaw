"""Focused tests for protected file mutation evidence and recovery checks."""

from __future__ import annotations

import os
import json
import stat
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.agent.database import Database
from app.agent.durable.journal import Journal
from app.agent.durable.recovery import OwnerProcessIdentity, recover_startup_runs
from app.agent.file_mutation.fingerprint import FingerprintError, fingerprint_path
from app.agent.file_mutation.planner import MutationPlan, PathState, PlanOperand, build_plan
from app.agent.file_mutation.snapshot_store import SnapshotStore, SnapshotStoreError
from app.agent.file_mutation.verifier import FileMutationVerifier
from app.agent.settings_store import AgentSettings, PathRule, save_agent_settings
from app.skills.filesystem import file_ops


def _state(path: Path, *, include_bytes: bool = False) -> tuple[PathState, bytes | None]:
    fingerprint = fingerprint_path(path, include_bytes=include_bytes)
    return PathState.from_fingerprint(fingerprint), fingerprint.data


def _write_plan(path: Path, before_bytes: bytes, after_bytes: bytes, *, strategy: str = "same") -> MutationPlan:
    before, observed = _state(path, include_bytes=True)
    assert observed == before_bytes
    after = PathState.from_bytes(
        path,
        after_bytes,
        identity=before.identity if strategy == "same" else None,
        parent_identity=before.parent_identity,
    )
    return build_plan(
        "write",
        {"path": str(path), "content": after_bytes.decode("utf-8", errors="replace")},
        [PlanOperand(str(path), "target", before, after, identity_strategy=strategy)],
        before_bytes=before_bytes,
        after_bytes=after_bytes,
        read_authorized=True,
    )


def _private_row(snapshot_ref: str, *, run_id: str = "run-evidence", operation_id: str = "op-evidence", kind: str = "file_write"):
    return {
        "run_id": run_id,
        "operation_id": operation_id,
        "operation_kind": kind,
        "effect_class": "verifiable_write",
        "status": "executing",
        "snapshot_ref": snapshot_ref,
    }


def _configure_delete_policy(tmp_path: Path, monkeypatch, root: Path, *, read: bool = True) -> None:
    import app.agent.controller_policy as controller_policy

    policy_dir = tmp_path / "delete-policy"
    policy_dir.mkdir()
    monkeypatch.setattr(controller_policy, "_POLICY_DIR", policy_dir)
    monkeypatch.setattr(controller_policy, "_POLICY_FILE", policy_dir / "controller_policy.md")
    monkeypatch.setattr(controller_policy, "_ALLOWLIST_FILE", policy_dir / "allowlisted_apps.md")
    settings = AgentSettings()
    settings.permissions.mode = "custom"
    settings.permissions.allow_delete = True
    settings.permissions.dangerous_actions_require_confirm = False
    settings.permissions.confirmations.delete = False
    settings.permissions.confirmations.mutate = False
    settings.permissions.path_rules = [
        PathRule(
            path=str(root),
            read=read,
            write=True,
            delete=True,
            launch=False,
            enabled=True,
        )
    ]
    save_agent_settings(settings, settings_path=policy_dir.parent / "settings.json")


class _FakeDeleteExecution:
    run_id = "run-delete-test"
    operation_id = "operation-delete-test"
    approved_ticket_id = ""
    approved_binding_digest = ""

    def __init__(self) -> None:
        self.snapshot_ref = ""
        self.no_effect_reason = ""

    def can_promote_native_file_mutation(self) -> bool:
        return True

    def promote_native_file_mutation(self, *, snapshot_ref: str, **_kwargs) -> None:
        self.snapshot_ref = snapshot_ref

    def assert_native_file_effect_allowed(self) -> None:
        return None

    def native_file_no_effect(self, reason: str) -> None:
        self.no_effect_reason = reason

    def native_file_verification_unavailable(self) -> None:
        return None

    def verify_native_file_mutation(self, verifier: FileMutationVerifier) -> dict[str, object]:
        return verifier.verify(
            {
                "run_id": self.run_id,
                "operation_id": self.operation_id,
                "operation_kind": "file_delete",
                "effect_class": "verifiable_write",
                "status": "executing",
                "snapshot_ref": self.snapshot_ref,
            }
        )


def test_fingerprint_rejects_special_and_hard_linked_paths(tmp_path: Path):
    ordinary = tmp_path / "ordinary.txt"
    ordinary.write_bytes(b"content")
    hard_link = tmp_path / "hard-link.txt"
    os.link(ordinary, hard_link)

    with pytest.raises(FingerprintError, match="hard-linked"):
        fingerprint_path(ordinary)

    with pytest.raises(FingerprintError, match="regular"):
        fingerprint_path(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows birthtime/ctime API compatibility")
@pytest.mark.parametrize("changed_during_read", [False, True])
def test_windows_fingerprint_uses_birthtime_across_apis_and_still_detects_handle_changes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_during_read: bool):
    target = tmp_path / "windows-time-identity.txt"
    target.write_bytes(b"stable file bytes")
    original_fstat, original_lstat = os.fstat, os.lstat
    calls = 0
    def normalize(value, *, handle=False):
        fields = {name: getattr(value, name) for name in dir(value) if name.startswith("st_")}
        birthtime = getattr(value, "st_birthtime_ns", value.st_ctime_ns)
        fields["st_birthtime_ns"] = birthtime
        fields["st_ctime_ns"] = birthtime + 1000000 if handle else birthtime
        return SimpleNamespace(**fields)
    def fstat(descriptor):
        nonlocal calls
        calls += 1
        value = normalize(original_fstat(descriptor), handle=True)
        if changed_during_read and calls > 1:
            value.st_ctime_ns += 100
        return value
    monkeypatch.setattr(os, "fstat", fstat)
    monkeypatch.setattr(os, "lstat", lambda *args, **kwargs: normalize(original_lstat(*args, **kwargs)))
    if changed_during_read:
        with pytest.raises(FingerprintError, match="changed while being read"):
            fingerprint_path(target, include_bytes=True)
    else:
        fingerprint = fingerprint_path(target, include_bytes=True)
        assert fingerprint.data == b"stable file bytes"
        assert fingerprint.created_ns == getattr(original_lstat(target), "st_birthtime_ns", original_lstat(target).st_ctime_ns)


def test_snapshot_store_publishes_private_bounded_manifest_and_binds_run_operation(tmp_path: Path):
    target = tmp_path / "private.txt"
    before = b"private before-image bytes"
    target.write_bytes(before)
    plan = _write_plan(target, before, b"after-image")
    store = SnapshotStore(tmp_path / "snapshot-store")

    snapshot_ref = store.capture(
        plan,
        {str(target): before},
        run_id="run-evidence",
        operation_id="op-evidence",
        authorized_roots=[str(target)],
    )

    assert snapshot_ref.startswith("snap-v1-")
    snapshot = store.load(snapshot_ref, run_id="run-evidence", operation_id="op-evidence")
    assert snapshot["operation_kind"] == "file_write"
    assert snapshot["before_bytes"][str(target)]
    stored_path = store.root / f"{snapshot_ref}.snap"
    assert before not in stored_path.read_bytes()
    if os.name != "nt":
        assert stat.S_IMODE(os.lstat(store.root).st_mode) == 0o700
        assert stat.S_IMODE(os.lstat(stored_path).st_mode) == 0o600
    with pytest.raises(SnapshotStoreError, match="run binding"):
        store.load(snapshot_ref, run_id="another-run", operation_id="op-evidence")
    with pytest.raises(SnapshotStoreError, match="operation binding"):
        store.load(snapshot_ref, run_id="run-evidence", operation_id="another-operation")


def test_write_recovery_proves_unchanged_and_completed_states_without_paths(tmp_path: Path):
    target = tmp_path / "notes.txt"
    original = b"original notes\n"
    updated = b"updated notes\n"
    target.write_bytes(original)
    plan = _write_plan(target, original, updated)
    store = SnapshotStore(tmp_path / "snapshots")
    snapshot_ref = store.capture(
        plan,
        {str(target): original},
        run_id="run-evidence",
        operation_id="op-evidence",
        authorized_roots=[str(target)],
    )
    verifier = FileMutationVerifier(store, read_policy=lambda _path: True)
    operation = _private_row(snapshot_ref)

    unchanged = verifier.verify(operation)
    assert unchanged["verification_result"] == "unchanged"
    assert str(target) not in repr(unchanged)
    assert original.decode() not in repr(unchanged)

    target.write_bytes(updated)
    completed = verifier.verify(operation)
    assert completed["verification_result"] == "completed"
    assert completed["sha256_matches"] is True
    assert str(target) not in repr(completed)
    assert updated.decode() not in repr(completed)

    target.write_bytes(b"mixed state")
    assert verifier.verify(operation)["verification_result"] == "unknown"


def test_recovery_rechecks_current_read_policy_before_fingerprinting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = tmp_path / "notes.txt"
    original = b"original"
    target.write_bytes(original)
    plan = _write_plan(target, original, b"next")
    store = SnapshotStore(tmp_path / "snapshots")
    snapshot_ref = store.capture(
        plan,
        {str(target): original},
        run_id="run-evidence",
        operation_id="op-evidence",
        authorized_roots=[str(target)],
    )
    monkeypatch.setattr(
        "app.agent.file_mutation.verifier.fingerprint_path",
        lambda *_args, **_kwargs: pytest.fail("fingerprinted after READ permission was revoked"),
    )
    verifier = FileMutationVerifier(store, read_policy=lambda _path: {
        "allowed": False,
        "requires_access_grant": True,
    })

    assert verifier.verify(_private_row(snapshot_ref))["verification_result"] == "unknown"


def test_move_recovery_requires_source_absent_and_destination_same_identity_and_hash(tmp_path: Path):
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    contents = b"same-volume move contents"
    source.write_bytes(contents)
    source_before, _ = _state(source, include_bytes=True)
    destination_before, _ = _state(destination)
    source_after = PathState.metadata(
        str(source),
        exists=False,
        kind="absent",
        size=0,
        parent_identity=source_before.parent_identity,
    )
    destination_after = PathState.from_bytes(
        destination,
        contents,
        identity=source_before.identity,
        modified_ns=source_before.modified_ns,
        created_ns=source_before.created_ns,
        parent_identity=destination_before.parent_identity,
    )
    plan = build_plan(
        "move",
        {"source": str(source), "destination": str(destination)},
        [
            PlanOperand(str(source), "source", source_before, source_after, identity_strategy="absent"),
            PlanOperand(str(destination), "destination", destination_before, destination_after, identity_strategy="replaced"),
        ],
    )
    store = SnapshotStore(tmp_path / "snapshots")
    snapshot_ref = store.capture(
        plan,
        {},
        run_id="run-evidence",
        operation_id="op-move",
        authorized_roots=[str(source), str(destination)],
    )
    verifier = FileMutationVerifier(store, read_policy=lambda _path: True)
    operation = _private_row(snapshot_ref, operation_id="op-move", kind="file_move")

    os.replace(source, destination)
    completed = verifier.verify(operation)
    assert completed["verification_result"] == "completed"
    assert completed["source_exists"] is False
    assert completed["destination_identity_matches"] is True

    source.write_bytes(contents)
    destination.unlink()
    destination.write_bytes(contents)
    assert verifier.verify(operation)["verification_result"] == "unknown"


def test_delete_requires_before_image_and_recovery_distinguishes_deleted_from_unchanged(tmp_path: Path):
    target = tmp_path / "remove.txt"
    original = b"delete me safely"
    target.write_bytes(original)
    before, observed = _state(target, include_bytes=True)
    after = PathState.metadata(
        str(target), exists=False, kind="absent", size=0, parent_identity=before.parent_identity
    )
    plan = build_plan(
        "delete",
        {"path": str(target)},
        [PlanOperand(str(target), "target", before, after, identity_strategy="absent")],
        before_bytes=original,
        read_authorized=True,
    )
    store = SnapshotStore(tmp_path / "snapshots")
    snapshot_ref = store.capture(
        plan,
        {str(target): observed},
        run_id="run-evidence",
        operation_id="op-delete",
        authorized_roots=[str(target)],
    )
    verifier = FileMutationVerifier(store, read_policy=lambda _path: True)
    operation = _private_row(snapshot_ref, operation_id="op-delete", kind="file_delete")

    assert verifier.verify(operation)["verification_result"] == "unchanged"
    target.unlink()
    assert verifier.verify(operation)["verification_result"] == "completed"


def test_fs_delete_uses_snapshot_verification_and_dry_run_never_captures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "remove.txt"
    target.write_bytes(b"private delete preview\n")
    _configure_delete_policy(tmp_path, monkeypatch, root)
    store = SnapshotStore(tmp_path / "snapshots")
    monkeypatch.setattr(file_ops, "_p3_snapshot_store", lambda: store)

    def reject_capture(*_args, **_kwargs):
        pytest.fail("delete Dry Run must not create a snapshot")

    with monkeypatch.context() as dry_run_patch:
        dry_run_patch.setattr(SnapshotStore, "capture", reject_capture)
        dry_run = file_ops.fs_delete(str(target), recursive=False, dry_run=True)
    preview = json.loads(dry_run)
    assert preview["status"] == "ok"
    assert preview["preview"]["operation"] == "delete"
    assert target.read_bytes() == b"private delete preview\n"

    execution = _FakeDeleteExecution()
    monkeypatch.setattr(file_ops, "_p3_snapshot_store", lambda: store)
    monkeypatch.setattr(file_ops, "current_operation_execution", lambda: execution)

    result = json.loads(file_ops.fs_delete(str(target), recursive=False))
    assert result["status"] == "ok"
    assert result["verification_result"] == "completed"
    assert not target.exists()
    assert execution.snapshot_ref


def test_fs_delete_snapshot_failure_keeps_the_target_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "remove.txt"
    original = b"do not remove without a committed snapshot"
    target.write_bytes(original)
    _configure_delete_policy(tmp_path, monkeypatch, root)
    execution = _FakeDeleteExecution()

    class _UnavailableStore:
        def capture(self, *_args, **_kwargs):
            raise SnapshotStoreError("publication failed")

    monkeypatch.setattr(file_ops, "current_operation_execution", lambda: execution)
    monkeypatch.setattr(file_ops, "_p3_snapshot_store", lambda: _UnavailableStore())

    result = json.loads(file_ops.fs_delete(str(target), recursive=False))
    assert result["status"] == "error"
    assert result["reason_code"] == "snapshot_unavailable"
    assert target.read_bytes() == original
    assert execution.no_effect_reason == "operation_failed"


def test_cleanup_keeps_recoverable_references_and_removes_old_orphans(tmp_path: Path):
    target = tmp_path / "notes.txt"
    original = b"before"
    target.write_bytes(original)
    plan = _write_plan(target, original, b"after")
    store = SnapshotStore(tmp_path / "snapshots")
    refs = [
        store.capture(
            plan,
            {str(target): original},
            run_id="run-evidence",
            operation_id=f"op-{index}",
            authorized_roots=[str(target)],
        )
        for index in range(2)
    ]
    old = 1_700_000_000
    for reference in refs:
        os.utime(store.root / f"{reference}.snap", (old, old))

    result = store.cleanup({refs[0]}, now=datetime.fromtimestamp(1_800_000_000, tz=timezone.utc))

    assert result["snapshot_files_deleted"] == 1
    assert (store.root / f"{refs[0]}.snap").exists()
    assert not (store.root / f"{refs[1]}.snap").exists()


def test_capture_fails_closed_when_before_image_exceeds_per_file_quota(tmp_path: Path):
    target = tmp_path / "large.txt"
    contents = b"x" * (32 * 1024)
    target.write_bytes(contents)
    plan = _write_plan(target, contents, b"short")
    store = SnapshotStore(tmp_path / "snapshots", max_before_file_bytes=1024)

    with pytest.raises(SnapshotStoreError, match="safely fingerprinted"):
        store.capture(
            plan,
            {str(target): contents},
            run_id="run-evidence",
            operation_id="op-large",
            authorized_roots=[str(target)],
        )


def test_created_write_without_expected_identity_stays_opaque(tmp_path: Path):
    target = tmp_path / "new.txt"
    before, _ = _state(target)
    after = PathState.from_bytes(target, b"new content", parent_identity=before.parent_identity)
    plan = build_plan(
        "write",
        {"path": str(target), "content": "new content"},
        [PlanOperand(str(target), "target", before, after, identity_strategy="replaced")],
    )
    store = SnapshotStore(tmp_path / "snapshots")

    with pytest.raises(SnapshotStoreError, match="expected post-state identity"):
        store.capture(
            plan,
            {},
            run_id="run-evidence",
            operation_id="op-create",
            authorized_roots=[str(target)],
        )


def test_startup_recovery_uses_default_native_verifier_and_preserves_active_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import app.agent.file_mutation.snapshot_store as snapshot_store_module
    import app.agent.file_mutation.verifier as verifier_module

    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(snapshot_store_module, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(snapshot_store_module, "_DEFAULT_STORE", None)
    monkeypatch.setattr(verifier_module, "_DEFAULT_VERIFIER", None)
    monkeypatch.setattr(verifier_module, "_current_read_policy", lambda _path: True)

    db = Database(tmp_path / "agent.db")
    db.init_db()
    journal = Journal(db)
    old_owner = "owner-v1-" + "e" * 32
    recovery_owner = OwnerProcessIdentity(tmp_path / "recovery-locks")
    recovery_id = recovery_owner.acquire()
    target = tmp_path / "workspace" / "notes.txt"
    target.parent.mkdir()
    before_bytes = b"before"
    after_bytes = b"after"
    target.write_bytes(before_bytes)
    plan = _write_plan(target, before_bytes, after_bytes)
    store = SnapshotStore(runtime_dir / "file_mutation" / "snapshots")
    snapshot_ref = store.capture(
        plan,
        {str(target): before_bytes},
        run_id="run-startup-evidence",
        operation_id="op-startup-evidence",
        authorized_roots=[str(target)],
    )

    try:
        db.create_conversation("chat-startup-evidence", "Recovery", str(target.parent))
        created = journal.create_run(
            run_id="run-startup-evidence",
            conversation_id="chat-startup-evidence",
            workspace_dir=str(target.parent),
            execution_source="desktop",
            owner_instance_id=old_owner,
            turn_id="turn-startup-evidence",
        )
        operation = journal.prepare_operation(
            "run-startup-evidence",
            operation_id="op-startup-evidence",
            owner_instance_id=old_owner,
            attempt=int(created["attempt"]),
            operation_kind="file_write",
            effect_class="opaque_effect",
        )
        journal.transition_operation(
            "run-startup-evidence", operation["operation_id"], to_status="authorized",
            owner_instance_id=old_owner, attempt=1,
        )
        journal.transition_operation(
            "run-startup-evidence", operation["operation_id"], to_status="executing",
            owner_instance_id=old_owner, attempt=1,
        )
        journal.promote_operation_to_verifiable_write(
            "run-startup-evidence",
            operation["operation_id"],
            owner_instance_id=old_owner,
            attempt=1,
            snapshot_ref=snapshot_ref,
            input_summary={"snapshot_available": True},
            precondition_evidence={"verification_result": "not_checked"},
        )
        target.write_bytes(after_bytes)

        report = recover_startup_runs(
            journal=journal,
            recovery_owner_instance_id=recovery_id,
            owner_proof=lambda _owner: "dead",
        )

        assert report["verified_operations"] == 1
        assert journal.load_operation("run-startup-evidence", "op-startup-evidence")["status"] == "verified"
        assert (store.root / f"{snapshot_ref}.snap").exists()
    finally:
        recovery_owner.close()
        db.close()
