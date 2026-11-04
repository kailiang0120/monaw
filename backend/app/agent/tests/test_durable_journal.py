from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.agent.database import Database
from app.agent.durable import (
    IdempotencyConflictError,
    InvalidTransitionError,
    Journal,
    JournalConflictError,
)


def _new_database(path, *, conversation_id="journal-chat"):
    db = Database(path)
    db.init_db()
    db.create_conversation(conversation_id, "Journal chat", workspace_dir="C:/work")
    return db


def _create_run(journal: Journal, *, run_id="run-journal", owner="owner-a", conversation_id="journal-chat"):
    turn_id = f"turn-{run_id}-1"
    created = journal.create_run(
        run_id=run_id,
        conversation_id=conversation_id,
        execution_source="desktop",
        owner_instance_id=owner,
        turn_id=turn_id,
        workspace_dir="C:/work",
    )
    return {**created, "turn_id": turn_id}


def _commit_resume_input(db: Database, created):
    db.persist_turn_messages(
        created["turn_id"],
        created["conversation_id"],
        [{"role": "user", "content": "original request"}, {"role": "assistant", "content": "checkpoint"}],
    )


def test_parallel_operation_prepares_have_monotonic_sequences_and_read_back_from_snapshot(tmp_path):
    db_a = _new_database(tmp_path / "agent.db")
    db_b = Database(db_a._path)
    db_b.init_db()
    journal_a = Journal(db_a)
    journal_b = Journal(db_b)
    created = _create_run(journal_a)

    retry_args = dict(
        run_id="run-journal",
        operation_id="op-retry",
        owner_instance_id="owner-a",
        attempt=created["attempt"],
        operation_kind="file_mutation",
        effect_class="verifiable_write",
        evidence={"target_exists": True, "verification_result": "not_checked"},
    )
    first = journal_a.prepare_operation(**retry_args)
    retry = journal_a.prepare_operation(**retry_args)
    assert retry["operation_id"] == first["operation_id"]
    assert retry["sequence"] == first["sequence"]

    collision_args = {**retry_args, "operation_kind": "file_delete"}
    with pytest.raises(IdempotencyConflictError):
        journal_a.prepare_operation(**collision_args)

    worker_count = 8
    barrier = threading.Barrier(worker_count)

    def prepare_parallel(index: int):
        # The barrier is outside Journal's transaction so both SQLite connections
        # compete to allocate the next operation and event sequence.
        barrier.wait(timeout=5)
        journal = journal_a if index % 2 == 0 else journal_b
        return journal.prepare_operation(
            run_id="run-journal",
            operation_id=f"op-parallel-{index}",
            owner_instance_id="owner-a",
            attempt=created["attempt"],
            operation_kind="tool_call",
            effect_class="read_only",
            evidence={"target_count": index},
        )

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        parallel = list(pool.map(prepare_parallel, range(worker_count)))

    operation_sequences = sorted([first["sequence"], *(item["sequence"] for item in parallel)])
    assert operation_sequences == list(range(1, worker_count + 2))

    first_operation_page = journal_a.list_operations("run-journal", limit=3, after_sequence=0)
    second_operation_page = journal_b.list_operations(
        "run-journal", limit=3, after_sequence=first_operation_page[-1]["sequence"]
    )
    assert [item["sequence"] for item in first_operation_page] == [1, 2, 3]
    assert [item["sequence"] for item in second_operation_page] == [4, 5, 6]

    snapshot = journal_a.snapshot("run-journal")
    page = journal_b.read_events("run-journal", after_seq=0, limit=100)
    event_sequences = [event["seq"] for event in page["events"]]
    assert event_sequences == list(range(1, snapshot["last_seq"] + 1))
    assert page["last_seq"] == snapshot["last_seq"]
    assert page["next_seq"] == snapshot["last_seq"]
    assert journal_a.read_events("run-journal", after_seq=snapshot["last_seq"])["events"] == []

    db_b.close()
    db_a.close()


def test_claim_race_across_database_objects_has_one_winner(tmp_path):
    db_a = _new_database(tmp_path / "agent.db")
    journal_a = Journal(db_a)
    created = _create_run(journal_a)
    _commit_resume_input(db_a, created)
    pausing = journal_a.transition_run(
        "run-journal",
        to_status="pausing",
        owner_instance_id="owner-a",
        attempt=created["attempt"],
        expected_revision=created["revision"],
    )
    paused = journal_a.transition_run(
        "run-journal",
        to_status="paused",
        owner_instance_id="owner-a",
        attempt=created["attempt"],
        expected_revision=pausing["revision"],
        recovery_input_ref=f"turn_checkpoint:{created['turn_id']}",
    )

    db_b = Database(db_a._path)
    db_b.init_db()
    journal_b = Journal(db_b)
    barrier = threading.Barrier(2)

    def claim(journal: Journal, owner: str):
        barrier.wait(timeout=5)
        try:
            return journal.claim_run(
                "run-journal",
                owner_instance_id=owner,
                expected_revision=paused["revision"],
                expected_attempt=created["attempt"],
                turn_id=f"turn-claim-{owner}",
            )
        except JournalConflictError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda pair: claim(*pair), ((journal_a, "owner-b"), (journal_b, "owner-c"))))

    winners = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, JournalConflictError)]
    assert len(winners) == 1
    assert len(conflicts) == 1
    assert winners[0]["attempt"] == created["attempt"] + 1
    assert db_a.fetchone("SELECT COUNT(*) FROM run_attempts WHERE run_id = 'run-journal'")[0] == 2

    db_b.close()
    db_a.close()


def test_stale_owner_attempt_and_expired_lease_do_not_enable_takeover(tmp_path):
    db = _new_database(tmp_path / "agent.db")
    journal = Journal(db)
    created = _create_run(journal)
    db.execute(
        "UPDATE agent_runs SET lease_expires_at = ? WHERE run_id = ?",
        ("2000-01-01T00:00:00+00:00", "run-journal"),
    )
    db.commit()

    with pytest.raises(JournalConflictError):
        journal.claim_run(
            "run-journal",
            owner_instance_id="owner-b",
            expected_revision=created["revision"],
            expected_attempt=created["attempt"],
            turn_id="turn-unsafe-takeover",
        )
    with pytest.raises(JournalConflictError):
        journal.renew_lease("run-journal", owner_instance_id="stale-owner", attempt=created["attempt"])
    with pytest.raises(JournalConflictError):
        journal.renew_lease("run-journal", owner_instance_id="owner-a", attempt=created["attempt"] + 1)

    current = journal.load_run("run-journal")
    assert current["status"] == "running"
    assert current["attempt"] == created["attempt"]
    assert db.fetchone("SELECT owner_instance_id FROM agent_runs WHERE run_id = 'run-journal'")[0] == "owner-a"
    db.close()


def test_expired_owner_recovery_classifies_in_flight_operations_and_handoffs_safely(tmp_path):
    db = _new_database(tmp_path / "agent.db")
    journal = Journal(db)
    created = _create_run(journal)
    _commit_resume_input(db, created)

    executing = journal.prepare_operation(
        "run-journal",
        operation_id="op-executing",
        owner_instance_id="owner-a",
        attempt=created["attempt"],
        operation_kind="file_write",
        effect_class="verifiable_write",
        evidence={"before_sha256": "a" * 64},
    )
    executing = journal.transition_operation(
        "run-journal", "op-executing", to_status="authorized", owner_instance_id="owner-a", attempt=1
    )
    journal.transition_operation(
        "run-journal", "op-executing", to_status="executing", owner_instance_id="owner-a", attempt=1
    )
    authorized = journal.prepare_operation(
        "run-journal",
        operation_id="op-authorized",
        owner_instance_id="owner-a",
        attempt=created["attempt"],
        operation_kind="file_move",
        effect_class="verifiable_write",
    )
    authorized = journal.transition_operation(
        "run-journal", "op-authorized", to_status="authorized", owner_instance_id="owner-a", attempt=1
    )
    prepared = journal.prepare_operation(
        "run-journal",
        operation_id="op-prepared",
        owner_instance_id="owner-a",
        attempt=created["attempt"],
        operation_kind="file_delete",
        effect_class="verifiable_write",
    )

    before_release = journal.load_run("run-journal")
    db.execute(
        "UPDATE agent_runs SET lease_expires_at = ? WHERE run_id = ?",
        ("2000-01-01T00:00:00+00:00", "run-journal"),
    )
    db.commit()
    with pytest.raises(JournalConflictError, match="prove the owner process is dead"):
        journal.release_expired_run(
            "run-journal",
            expected_owner_instance_id="owner-a",
            recovery_owner_instance_id="recovery-b",
            expected_attempt=created["attempt"],
            expected_revision=before_release["revision"],
            safe_to_release=True,
            owner_proven_dead=False,
        )
    interrupted = journal.release_expired_run(
        "run-journal",
        expected_owner_instance_id="owner-a",
        recovery_owner_instance_id="recovery-b",
        expected_attempt=created["attempt"],
        expected_revision=before_release["revision"],
        safe_to_release=True,
        owner_proven_dead=True,
    )

    assert interrupted["status"] == "interrupted"
    assert journal.load_operation("run-journal", "op-executing")["status"] == "uncertain"
    assert journal.load_operation("run-journal", "op-authorized")["status"] == "cancelled"
    assert journal.load_operation("run-journal", "op-prepared")["status"] == "cancelled"
    assert db.fetchone("SELECT owner_instance_id FROM agent_runs WHERE run_id = 'run-journal'")[0] == "recovery-b"
    with pytest.raises(JournalConflictError):
        journal.renew_lease("run-journal", owner_instance_id="owner-a", attempt=created["attempt"])
    with pytest.raises(InvalidTransitionError):
        journal.prepare_operation(
            "run-journal",
            operation_id="op-must-not-dispatch",
            owner_instance_id="recovery-b",
            attempt=created["attempt"],
            operation_kind="tool_call",
            effect_class="read_only",
        )
    assert journal.load_operation("run-journal", "op-must-not-dispatch") is None

    recovery_state = journal.load_run("run-journal")
    needs_review = journal.transition_run(
        "run-journal",
        to_status="needs_review",
        owner_instance_id="recovery-b",
        attempt=created["attempt"],
        expected_revision=recovery_state["revision"],
        reason_code="external_effect_unknown",
    )
    db.execute(
        "UPDATE agent_runs SET lease_expires_at = ? WHERE run_id = ?",
        ("2000-01-01T00:00:00+00:00", "run-journal"),
    )
    db.commit()
    handed_off = journal.release_expired_run(
        "run-journal",
        expected_owner_instance_id="recovery-b",
        recovery_owner_instance_id="recovery-c",
        expected_attempt=created["attempt"],
        expected_revision=needs_review["revision"],
        safe_to_release=True,
        owner_proven_dead=True,
    )
    assert handed_off["status"] == "needs_review"
    assert db.fetchone("SELECT owner_instance_id FROM agent_runs WHERE run_id = 'run-journal'")[0] == "recovery-c"
    with pytest.raises(JournalConflictError):
        journal.renew_lease("run-journal", owner_instance_id="recovery-b", attempt=created["attempt"])

    with pytest.raises(InvalidTransitionError):
        journal.transition_operation(
            "run-journal",
            "op-executing",
            to_status="verified",
            owner_instance_id="recovery-c",
            attempt=created["attempt"],
            evidence={"after_sha256": "b" * 64},
        )
    verified = journal.transition_operation(
        "run-journal",
        "op-executing",
        to_status="verified",
        owner_instance_id="recovery-c",
        attempt=created["attempt"],
        evidence={"verification_result": "completed", "after_sha256": "b" * 64},
    )
    assert verified["status"] == "verified"

    review_resolved = journal.load_run("run-journal")
    paused = journal.transition_run(
        "run-journal",
        to_status="paused",
        owner_instance_id="recovery-c",
        attempt=created["attempt"],
        expected_revision=review_resolved["revision"],
        recovery_input_ref=f"turn_checkpoint:{created['turn_id']}",
    )
    assert paused["status"] == "paused"
    assert db.fetchone("SELECT owner_instance_id FROM agent_runs WHERE run_id = 'run-journal'")[0] is None

    claimed = journal.claim_run(
        "run-journal",
        owner_instance_id="owner-c",
        expected_revision=paused["revision"],
        expected_attempt=created["attempt"],
        turn_id="turn-recovery-claim",
    )
    assert claimed["status"] == "running"
    assert claimed["attempt"] == created["attempt"] + 1
    with pytest.raises(JournalConflictError):
        journal.renew_lease("run-journal", owner_instance_id="owner-a", attempt=created["attempt"])
    db.close()


def test_verifiable_write_needs_new_post_write_evidence(tmp_path):
    db = _new_database(tmp_path / "agent.db")
    journal = Journal(db)
    created = _create_run(journal)
    before_hash = "a" * 64
    operation = journal.prepare_operation(
        "run-journal",
        operation_id="op-write-verify",
        owner_instance_id="owner-a",
        attempt=created["attempt"],
        operation_kind="file_write",
        effect_class="verifiable_write",
        evidence={"before_sha256": before_hash},
    )
    journal.transition_operation(
        "run-journal", operation["operation_id"], to_status="authorized", owner_instance_id="owner-a", attempt=1
    )
    journal.transition_operation(
        "run-journal", operation["operation_id"], to_status="executing", owner_instance_id="owner-a", attempt=1
    )

    with pytest.raises(InvalidTransitionError):
        journal.transition_operation(
            "run-journal",
            operation["operation_id"],
            to_status="verified",
            owner_instance_id="owner-a",
            attempt=1,
            evidence={"after_sha256": before_hash},
        )
    with pytest.raises(InvalidTransitionError):
        journal.transition_operation(
            "run-journal",
            operation["operation_id"],
            to_status="verified",
            owner_instance_id="owner-a",
            attempt=1,
            evidence={"target_exists": True},
        )
    assert journal.load_operation("run-journal", operation["operation_id"])["status"] == "executing"

    completed = journal.transition_operation(
        "run-journal",
        operation["operation_id"],
        to_status="verified",
        owner_instance_id="owner-a",
        attempt=1,
        evidence={"verification_result": "completed", "after_sha256": "b" * 64},
    )
    assert completed["status"] == "verified"
    db.close()


def test_verified_operation_retry_matches_original_preconditions_without_new_event(tmp_path):
    db = _new_database(tmp_path / "agent.db")
    journal = Journal(db)
    created = _create_run(journal)
    original_args = {
        "run_id": "run-journal",
        "operation_id": "op-verified-retry",
        "owner_instance_id": "owner-a",
        "attempt": created["attempt"],
        "operation_kind": "file_write",
        "effect_class": "verifiable_write",
        "input_summary": {"byte_length": 10},
        "evidence": {"before_sha256": "a" * 64, "target_exists": True},
        "snapshot_ref": "snapshot-1",
    }

    prepared = journal.prepare_operation(**original_args)
    journal.transition_operation(
        "run-journal", prepared["operation_id"], to_status="authorized", owner_instance_id="owner-a", attempt=1
    )
    journal.transition_operation(
        "run-journal", prepared["operation_id"], to_status="executing", owner_instance_id="owner-a", attempt=1
    )
    verified = journal.transition_operation(
        "run-journal",
        prepared["operation_id"],
        to_status="verified",
        owner_instance_id="owner-a",
        attempt=1,
        evidence={"verification_result": "completed", "after_sha256": "b" * 64},
    )
    assert verified["status"] == "verified"

    last_seq = journal.snapshot("run-journal")["last_seq"]
    event_count = db.fetchone("SELECT COUNT(*) FROM run_events WHERE run_id = 'run-journal'")[0]
    retry = journal.prepare_operation(**original_args)

    assert retry["status"] == "verified"
    assert retry["sequence"] == prepared["sequence"]
    assert journal.snapshot("run-journal")["last_seq"] == last_seq
    assert db.fetchone("SELECT COUNT(*) FROM run_events WHERE run_id = 'run-journal'")[0] == event_count

    changed_precondition = {**original_args, "evidence": {"before_sha256": "c" * 64, "target_exists": True}}
    with pytest.raises(IdempotencyConflictError):
        journal.prepare_operation(**changed_precondition)
    changed_snapshot = {**original_args, "snapshot_ref": "snapshot-2"}
    with pytest.raises(IdempotencyConflictError):
        journal.prepare_operation(**changed_snapshot)
    assert journal.snapshot("run-journal")["last_seq"] == last_seq
    db.close()


def test_run_attempt_checkpoint_is_deduplicated_and_remains_conversation_bound(tmp_path):
    db = _new_database(tmp_path / "agent.db")
    journal = Journal(db)
    created = _create_run(journal)
    turn_id = "turn-run-journal-1"
    messages = [{"role": "user", "content": "persist once"}, {"role": "assistant", "content": "done"}]

    user_ids, inserted = db.persist_turn_messages(turn_id, "journal-chat", messages)
    retry_ids, inserted_on_retry = db.persist_turn_messages(
        turn_id, "journal-chat", [{"role": "user", "content": "different retry body"}]
    )
    checkpoint = journal.load_turn_checkpoint("run-journal", created["attempt"])

    assert inserted is True
    assert inserted_on_retry is False
    assert retry_ids == user_ids
    assert checkpoint["turn_id"] == turn_id
    assert checkpoint["conversation_id"] == "journal-chat"
    assert checkpoint["user_message_ids"] == user_ids

    db.create_conversation("other-chat", "Other chat")
    with pytest.raises(ValueError, match="another conversation"):
        db.persist_turn_messages(turn_id, "other-chat", [{"role": "user", "content": "cannot rebind"}])
    assert db.get_messages("other-chat") == []
    db.close()


def test_event_insert_failure_rolls_back_operation_and_run_cursor(tmp_path):
    db = _new_database(tmp_path / "agent.db")
    journal = Journal(db)
    created = _create_run(journal)
    before = journal.snapshot("run-journal")
    db.execute(
        """
        CREATE TEMP TRIGGER fail_run_event
        BEFORE INSERT ON run_events
        BEGIN
            SELECT RAISE(ABORT, 'forced event insert failure');
        END
        """
    )
    db.commit()

    with pytest.raises(sqlite3.IntegrityError, match="forced event insert failure"):
        journal.prepare_operation(
            run_id="run-journal",
            operation_id="op-atomic-failure",
            owner_instance_id="owner-a",
            attempt=created["attempt"],
            operation_kind="file_write",
            effect_class="verifiable_write",
        )

    after = journal.snapshot("run-journal")
    assert after["revision"] == before["revision"]
    assert after["last_seq"] == before["last_seq"]
    assert db.fetchone("SELECT 1 FROM run_operations WHERE operation_id = 'op-atomic-failure'") is None
    assert db.fetchone("SELECT COUNT(*) FROM run_events WHERE run_id = 'run-journal'")[0] == before["last_seq"]

    db.execute("DROP TRIGGER fail_run_event")
    db.commit()
    committed = journal.prepare_operation(
        run_id="run-journal",
        operation_id="op-atomic-failure",
        owner_instance_id="owner-a",
        attempt=created["attempt"],
        operation_kind="file_write",
        effect_class="verifiable_write",
    )
    assert committed["sequence"] == 1
    db.close()


def test_raw_secrets_and_arbitrary_inputs_never_reach_sqlite_or_event_payload(tmp_path):
    db = _new_database(tmp_path / "agent.db")
    journal = Journal(db)
    created = _create_run(journal)
    secret = "monaw-test-token-raw-value-should-never-persist"
    operation = journal.prepare_operation(
        run_id="run-journal",
        operation_id="op-redacted",
        owner_instance_id="owner-a",
        attempt=created["attempt"],
        operation_kind="shell_command",
        effect_class="opaque_effect",
        evidence={
            "verification_result": "completed",
            "target_exists": True,
            "token": secret,
            "command": f"curl -H 'Authorization: Bearer {secret}'",
            "path": f"C:/private/{secret}.txt",
        },
    )

    prepared_row = db.fetchone(
        "SELECT precondition_json, evidence_json FROM run_operations WHERE operation_id = ?",
        (operation["operation_id"],),
    )
    assert json.loads(prepared_row["precondition_json"]) == {
        "verification_result": "completed",
        "target_exists": True,
    }
    assert json.loads(prepared_row["evidence_json"]) == {}
    assert operation["evidence"] == {}

    journal.transition_operation(
        "run-journal", operation["operation_id"], to_status="authorized", owner_instance_id="owner-a", attempt=1
    )
    journal.transition_operation(
        "run-journal", operation["operation_id"], to_status="executing", owner_instance_id="owner-a", attempt=1
    )
    returned = journal.transition_operation(
        "run-journal",
        operation["operation_id"],
        to_status="returned",
        owner_instance_id="owner-a",
        attempt=1,
        evidence={
            "verification_result": "unknown",
            "target_exists": True,
            "token": secret,
            "command": f"curl -H 'Authorization: Bearer {secret}'",
            "path": f"C:/private/{secret}.txt",
        },
    )
    assert returned["evidence"] == {"verification_result": "unknown", "target_exists": True}

    raw_operation_rows = db.fetchall(
        "SELECT input_summary_json, precondition_json, evidence_json FROM run_operations WHERE operation_id = ?",
        (operation["operation_id"],),
    )
    raw_event_rows = db.fetchall("SELECT payload_json FROM run_events WHERE run_id = ?", ("run-journal",))
    persisted_json = json.dumps(
        {
            "operations": [dict(row) for row in raw_operation_rows],
            "events": [dict(row) for row in raw_event_rows],
        }
    )

    assert secret not in persisted_json
    assert "Authorization" not in persisted_json
    assert "C:/private/" not in persisted_json
    persisted_operation = db.fetchone(
        "SELECT precondition_json, evidence_json FROM run_operations WHERE operation_id = ?",
        (operation["operation_id"],),
    )
    assert json.loads(persisted_operation["precondition_json"]) == {
        "verification_result": "completed",
        "target_exists": True,
    }
    assert json.loads(persisted_operation["evidence_json"]) == {
        "verification_result": "unknown",
        "target_exists": True,
    }
    last_event_payload = json.loads(
        db.fetchone(
            "SELECT payload_json FROM run_events WHERE run_id = ? ORDER BY seq DESC LIMIT 1",
            ("run-journal",),
        )[0]
    )
    assert last_event_payload["evidence"] == {"verification_result": "unknown", "target_exists": True}
    db.close()


def test_recovery_input_is_checkpointed_before_effects_and_finalized_once(tmp_path):
    db = _new_database(tmp_path / "agent.db")
    journal = Journal(db)
    created = _create_run(journal)

    early = journal.persist_recovery_input(
        "run-journal",
        owner_instance_id="owner-a",
        attempt=1,
        turn_id=created["turn_id"],
        conversation_id=created["conversation_id"],
        user_message="original request",
        expected_revision=int(created["revision"]),
    )
    assert early["inserted"] is True
    assert early["run"]["revision"] == int(created["revision"]) + 1
    assert db.fetchone(
        "SELECT updated_at FROM conversations WHERE id = ?", (created["conversation_id"],)
    )[0] == db.fetchone(
        "SELECT updated_at FROM agent_runs WHERE run_id = ?", ("run-journal",)
    )[0]
    checkpoint = db.fetchone(
        "SELECT user_message_ids, finalized FROM turn_checkpoints WHERE turn_id = ?",
        (created["turn_id"],),
    )
    assert checkpoint["finalized"] == 0
    original_user_ids = json.loads(checkpoint["user_message_ids"])

    messages = [
        {"role": "user", "content": "original request"},
        {"role": "assistant", "content": "completed", "tool_calls": [{
            "tool_name": "file_write", "input": "{}", "output": "verified", "status": "complete",
        }]},
    ]
    user_ids, inserted = db.persist_turn_messages(created["turn_id"], created["conversation_id"], messages)
    assert inserted is True
    assert user_ids == original_user_ids
    assert db.fetchone(
        "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND role = 'user'",
        (created["conversation_id"],),
    )[0] == 1
    assert db.fetchone(
        "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND role = 'assistant'",
        (created["conversation_id"],),
    )[0] == 1
    assert db.fetchone(
        "SELECT finalized FROM turn_checkpoints WHERE turn_id = ?", (created["turn_id"],)
    )[0] == 1
    assert db.fetchone(
        "SELECT COUNT(*) FROM tool_calls WHERE conversation_id = ?", (created["conversation_id"],)
    )[0] == 1

    retry_ids, retry_inserted = db.persist_turn_messages(
        created["turn_id"], created["conversation_id"], messages
    )
    assert retry_ids == original_user_ids
    assert retry_inserted is False
    assert db.fetchone(
        "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (created["conversation_id"],)
    )[0] == 2
    db.close()
