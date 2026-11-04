import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.agent.database import Database
from app.agent.durable.journal import Journal
from app.agent.durable.models import InvalidTransitionError, LeaseConflictError
from app.agent.durable.recovery import (
    OwnerProcessIdentity,
    assert_run_dispatch_allowed,
    get_owner_instance_id,
    owner_process_proof,
    recover_startup_runs,
    DispatchConflictError,
)


def _new_journal(tmp_path: Path) -> tuple[Database, Journal]:
    db = Database(tmp_path / "agent.db")
    db.init_db()
    journal = Journal(db)
    return db, journal


def _create_run(
    journal: Journal,
    *,
    owner: str,
    run_id: str = "recovery-run",
    turn_id: str = "recovery-turn",
    conversation_id: str = "recovery-chat",
    workspace_dir: str = "",
):
    journal.db.create_conversation(conversation_id, "Recovery", workspace_dir)
    return journal.create_run(
        run_id=run_id,
        conversation_id=conversation_id,
        workspace_dir=workspace_dir,
        execution_source="desktop",
        owner_instance_id=owner,
        turn_id=turn_id,
    )


def test_expired_lease_does_not_handoff_while_owner_lock_is_live(tmp_path):
    db, journal = _new_journal(tmp_path)
    live_owner = OwnerProcessIdentity(tmp_path / "owner-locks")
    recovery_owner = OwnerProcessIdentity(tmp_path / "recovery-locks")
    owner_id = live_owner.acquire()
    recovery_id = recovery_owner.acquire()
    created = _create_run(journal, owner=owner_id)
    db.execute(
        "UPDATE agent_runs SET lease_expires_at = ? WHERE run_id = ?",
        ("2000-01-01T00:00:00+00:00", "recovery-run"),
    )
    db.commit()

    assert owner_process_proof(owner_id, lock_dir=tmp_path / "owner-locks") == "live"
    assert owner_process_proof("owner-a", lock_dir=tmp_path / "owner-locks") == "unknown"
    assert owner_process_proof("owner-v1-" + "f" * 32, lock_dir=tmp_path / "owner-locks") == "unknown"
    report = recover_startup_runs(
        journal=journal,
        recovery_owner_instance_id=recovery_id,
        owner_proof=lambda value: owner_process_proof(value, lock_dir=tmp_path / "owner-locks"),
    )

    assert report["scanned"] == 1
    assert report["live_owner"] == 1
    assert report["recovered"] == 0
    current = journal.load_run_internal("recovery-run")
    assert current["owner_instance_id"] == owner_id
    assert current["attempt"] == created["attempt"]

    live_owner.close()
    recovery_owner.close()
    db.close()


def test_dead_process_handoff_marks_opaque_effect_needs_review_without_replay(tmp_path):
    lock_dir = tmp_path / "worker-locks"
    backend_dir = Path(__file__).resolve().parents[3]
    child_code = (
        "import sys, time; "
        "from pathlib import Path; "
        "from app.agent.durable.recovery import OwnerProcessIdentity; "
        "identity = OwnerProcessIdentity(Path(sys.argv[1])); "
        "print(identity.acquire(), flush=True); "
        "time.sleep(300)"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(backend_dir) + os.pathsep + environment.get("PYTHONPATH", "")
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", child_code, str(lock_dir)],
        cwd=backend_dir,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    recovery_owner = OwnerProcessIdentity(tmp_path / "recovery-locks")
    try:
        assert child.stdout is not None
        old_owner = child.stdout.readline().strip()
        assert old_owner.startswith("owner-v1-")
        assert owner_process_proof(old_owner, lock_dir=lock_dir) == "live"

        db, journal = _new_journal(tmp_path)
        created = _create_run(journal, owner=old_owner)
        operation = journal.prepare_operation(
            "recovery-run",
            operation_id="opaque-op",
            owner_instance_id=old_owner,
            attempt=int(created["attempt"]),
            operation_kind="tool_call",
            effect_class="opaque_effect",
        )
        journal.transition_operation(
            "recovery-run", operation["operation_id"], to_status="authorized", owner_instance_id=old_owner, attempt=1
        )
        journal.transition_operation(
            "recovery-run", operation["operation_id"], to_status="executing", owner_instance_id=old_owner, attempt=1
        )
        recovery_id = recovery_owner.acquire()

        child.terminate()
        child.wait(timeout=10)
        proof = owner_process_proof(old_owner, lock_dir=lock_dir)
        deadline = time.monotonic() + 2
        while proof == "live" and time.monotonic() < deadline:
            time.sleep(0.05)
            proof = owner_process_proof(old_owner, lock_dir=lock_dir)
        assert proof == "dead", f"expected dead owner proof after child exit, got {proof!r}"

        report = recover_startup_runs(
            journal=journal,
            recovery_owner_instance_id=recovery_id,
            owner_proof=lambda value: owner_process_proof(value, lock_dir=lock_dir),
        )

        recovered = journal.load_run_internal("recovery-run")
        assert report["recovered"] == 1
        assert report["proof_based_handoff"] == 1
        assert report["needs_review"] == 1
        assert recovered["status"] == "needs_review"
        assert recovered["owner_instance_id"] == recovery_id
        assert recovered["attempt"] == created["attempt"]
        assert journal.load_operation("recovery-run", "opaque-op")["status"] == "uncertain"
        assert db.fetchone("SELECT COUNT(*) FROM run_attempts WHERE run_id = ?", ("recovery-run",))[0] == 1
        assert db.fetchone("SELECT COUNT(*) FROM agent_runs")[0] == 1
        with pytest.raises(InvalidTransitionError):
            journal.prepare_operation(
                "recovery-run",
                operation_id="must-not-replay",
                owner_instance_id=recovery_id,
                attempt=int(recovered["attempt"]),
                operation_kind="tool_call",
                effect_class="opaque_effect",
            )
        db.close()
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
        recovery_owner.close()


def test_read_only_verifier_can_resolve_a_verifiable_write(tmp_path):
    db, journal = _new_journal(tmp_path)
    old_owner = "owner-v1-" + "b" * 32
    recovery_owner = OwnerProcessIdentity(tmp_path / "recovery-locks")
    created = _create_run(journal, owner=old_owner)
    operation = journal.prepare_operation(
        "recovery-run",
        operation_id="verifiable-op",
        owner_instance_id=old_owner,
        attempt=int(created["attempt"]),
        operation_kind="file_write",
        effect_class="verifiable_write",
        evidence={"before_sha256": "a" * 64},
    )
    journal.transition_operation(
        "recovery-run", operation["operation_id"], to_status="authorized", owner_instance_id=old_owner, attempt=1
    )
    journal.transition_operation(
        "recovery-run", operation["operation_id"], to_status="executing", owner_instance_id=old_owner, attempt=1
    )
    recovery_id = recovery_owner.acquire()
    verified_inputs = []

    report = recover_startup_runs(
        journal=journal,
        recovery_owner_instance_id=recovery_id,
        owner_proof=lambda _owner: "dead",
        read_only_verifier=lambda record: (
            verified_inputs.append(record) or {"verification_result": "completed", "after_sha256": "c" * 64}
        ),
    )

    assert report["verified_operations"] == 1
    assert verified_inputs[0]["operation_id"] == operation["operation_id"]
    assert journal.load_operation("recovery-run", operation["operation_id"])["status"] == "verified"
    assert journal.load_run("recovery-run")["status"] == "interrupted"
    recovery_owner.close()
    db.close()


def test_recovered_run_pauses_only_with_reconstructable_checkpoint(tmp_path):
    db, journal = _new_journal(tmp_path)
    old_owner = "owner-v1-" + "c" * 32
    recovery_owner = OwnerProcessIdentity(tmp_path / "recovery-locks")
    _create_run(journal, owner=old_owner, turn_id="checkpoint-turn")
    db.persist_turn_messages(
        "checkpoint-turn",
        "recovery-chat",
        [{"role": "user", "content": "Resume this safely"}],
    )
    recovery_id = recovery_owner.acquire()

    report = recover_startup_runs(
        journal=journal,
        recovery_owner_instance_id=recovery_id,
        owner_proof=lambda _owner: "dead",
    )

    recovered = journal.load_run_internal("recovery-run")
    assert report["paused"] == 1
    assert recovered["status"] == "paused"
    assert recovered["owner_instance_id"] == ""
    assert recovered["lease_expires_at"] == ""
    recovery_owner.close()
    db.close()


def test_resume_run_carries_verified_operation_evidence_into_new_attempt(tmp_path):
    from app.agent.durable.turn_run import resume_run

    db, journal = _new_journal(tmp_path)
    old_owner = "owner-v1-" + "e" * 32
    recovery_owner = OwnerProcessIdentity(tmp_path / "recovery-locks")
    created = _create_run(journal, owner=old_owner, turn_id="evidence-turn")
    journal.persist_recovery_input(
        "recovery-run",
        owner_instance_id=old_owner,
        attempt=1,
        turn_id="evidence-turn",
        conversation_id="recovery-chat",
        user_message="Write the requested file.",
        expected_revision=int(created["revision"]),
    )
    operation = journal.prepare_operation(
        "recovery-run",
        operation_id="verified-file-operation",
        owner_instance_id=old_owner,
        attempt=1,
        operation_kind="file_write",
        effect_class="verifiable_write",
        input_summary={"target_count": 1},
    )
    journal.transition_operation(
        "recovery-run", operation["operation_id"], to_status="authorized",
        owner_instance_id=old_owner, attempt=1,
    )
    journal.transition_operation(
        "recovery-run", operation["operation_id"], to_status="executing",
        owner_instance_id=old_owner, attempt=1,
    )
    recovery_id = recovery_owner.acquire()
    report = recover_startup_runs(
        journal=journal,
        recovery_owner_instance_id=recovery_id,
        owner_proof=lambda _owner: "dead",
        read_only_verifier=lambda _record: {
            "verification_result": "completed", "target_exists": True,
        },
    )
    assert report["paused"] == 1

    handle, checkpoint = resume_run(
        "recovery-run",
        execution_source="desktop",
        principal_id="",
        control_session_id="new-session",
        workspace_dir="",
        db=db,
    )
    assert checkpoint["user_message_ids"]
    assert handle.attempt == 2
    assert handle.recovery_evidence == [{
        "operation_id": "verified-file-operation",
        "operation_kind": "file_write",
        "status": "verified",
        "input_summary": {"target_count": 1},
        "evidence": {"verification_result": "completed", "target_exists": True},
    }]
    handle.finalize("error", "test_cleanup")
    recovery_owner.close()
    db.close()


def test_dispatch_guard_allows_same_process_workspace_siblings_and_blocks_cross_process_overlap(tmp_path):
    db, journal = _new_journal(tmp_path)
    current_owner = get_owner_instance_id()
    current = _create_run(
        journal,
        owner=current_owner,
        run_id="guard-current",
        turn_id="guard-turn-current",
        conversation_id="guard-chat-current",
        workspace_dir=str(tmp_path / "shared-workspace"),
    )
    sibling = _create_run(
        journal,
        owner=current_owner,
        run_id="guard-sibling",
        turn_id="guard-turn-sibling",
        conversation_id="guard-chat-sibling",
        workspace_dir=str(tmp_path / "shared-workspace"),
    )
    assert_run_dispatch_allowed(
        {
            "journal": journal,
            "run_id": "guard-current",
            "owner_instance_id": current_owner,
            "attempt": int(current["attempt"]),
        }
    )
    db.update_conversation("guard-chat-current", workspace_dir=str(tmp_path / "moved-workspace"))
    with pytest.raises(DispatchConflictError, match="workspace no longer matches"):
        assert_run_dispatch_allowed(
            {
                "journal": journal,
                "run_id": "guard-current",
                "owner_instance_id": current_owner,
                "attempt": int(current["attempt"]),
            }
        )
    db.update_conversation("guard-chat-current", workspace_dir=str(tmp_path / "shared-workspace"))
    assert_run_dispatch_allowed(
        {
            "journal": journal,
            "run_id": "guard-current",
            "owner_instance_id": current_owner,
            "attempt": int(current["attempt"]),
        }
    )

    sibling_op = journal.prepare_operation(
        "guard-sibling",
        operation_id="guard-sibling-op",
        owner_instance_id=current_owner,
        attempt=int(sibling["attempt"]),
        operation_kind="file_write",
        effect_class="verifiable_write",
    )
    journal.transition_operation(
        "guard-sibling",
        sibling_op["operation_id"],
        to_status="authorized",
        owner_instance_id=current_owner,
        attempt=int(sibling["attempt"]),
    )
    journal.transition_operation(
        "guard-sibling",
        sibling_op["operation_id"],
        to_status="executing",
        owner_instance_id=current_owner,
        attempt=int(sibling["attempt"]),
    )
    with pytest.raises(DispatchConflictError, match="another durable run"):
        assert_run_dispatch_allowed(
            {
                "journal": journal,
                "run_id": "guard-current",
                "owner_instance_id": current_owner,
                "attempt": int(current["attempt"]),
            }
        )
    journal.transition_operation(
        "guard-sibling",
        sibling_op["operation_id"],
        to_status="failed",
        owner_instance_id=current_owner,
        attempt=int(sibling["attempt"]),
        evidence={"verification_result": "unchanged"},
    )
    assert_run_dispatch_allowed(
        {
            "journal": journal,
            "run_id": "guard-current",
            "owner_instance_id": current_owner,
            "attempt": int(current["attempt"]),
        }
    )

    # A second process cannot insert a conflicting run row at all; the first
    # process retains its scope and can continue to dispatch after safe sibling work.
    other_owner = "owner-v1-" + "d" * 32
    with pytest.raises(LeaseConflictError, match="workspace has an active durable run"):
        _create_run(
            journal,
            owner=other_owner,
            run_id="guard-other-process-rejected",
            turn_id="guard-turn-other-rejected",
            conversation_id="guard-chat-other-rejected",
            workspace_dir=str(tmp_path / "shared-workspace"),
        )

    # Dispatch has an independent guard for scope drift after run creation:
    # create the second process in a distinct workspace, then simulate the
    # persisted scope moving into the current run's workspace.
    other_workspace = str(tmp_path / "other-workspace")
    _create_run(
        journal,
        owner=other_owner,
        run_id="guard-other-process-drift",
        turn_id="guard-turn-other-drift",
        conversation_id="guard-chat-other-drift",
        workspace_dir=other_workspace,
    )
    db.execute(
        "UPDATE agent_runs SET workspace_dir = ? WHERE run_id = ?",
        (str(tmp_path / "shared-workspace"), "guard-other-process-drift"),
    )
    db.update_conversation("guard-chat-other-drift", workspace_dir=str(tmp_path / "shared-workspace"))
    db.commit()
    with pytest.raises(DispatchConflictError, match="another durable run"):
        assert_run_dispatch_allowed(
            {
                "journal": journal,
                "run_id": "guard-current",
                "owner_instance_id": current_owner,
                "attempt": int(current["attempt"]),
            }
        )
    assert sibling["status"] == "running"
    db.close()
