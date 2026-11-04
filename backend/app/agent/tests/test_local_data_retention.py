from __future__ import annotations

import asyncio
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.api.routes import conversations
from app.agent.database import ConversationHasUnresolvedRunError, Database
from app.agent.durable.event_store import EventsExpiredError
from app.agent.durable.journal import Journal
from app.agent.file_mutation.snapshot_store import SnapshotStore, reclaim_pending_snapshot_refs


def _terminal_run_with_snapshot(
    db: Database,
    *,
    run_id: str,
    conversation_id: str,
    snapshot_ref: str,
    status: str = "completed",
    operation_status: str = "verified",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """INSERT INTO agent_runs (
               run_id, conversation_id, workspace_dir, execution_source,
               attempt, revision, last_seq, status, created_at, updated_at, finished_at
           ) VALUES (?, ?, '', 'desktop', 1, 1, 0, ?, ?, ?, ?)""",
        (run_id, conversation_id, status, now, now, now if status in {"completed", "failed", "cancelled"} else None),
    )
    db.execute(
        "INSERT INTO run_attempts (run_id, attempt, turn_id, created_at) VALUES (?, 1, ?, ?)",
        (run_id, f"turn-{run_id}", now),
    )
    db.execute(
        """INSERT INTO run_operations (
               operation_id, run_id, attempt, sequence_no, tool_name, operation_kind,
               effect_class, status, snapshot_ref, created_at, updated_at
           ) VALUES (?, ?, 1, 1, 'file_mutation', 'file_write', 'verifiable_write', ?, ?, ?, ?)""",
        (f"op-{run_id}", run_id, operation_status, snapshot_ref, now, now),
    )
    db.commit()


def _new_snapshot_ref() -> str:
    return f"snap-v1-{uuid.uuid4().hex}"


def test_conversation_delete_reclaims_only_exclusive_snapshot_after_cascade(tmp_path, monkeypatch):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    db.create_conversation("delete-me", "Delete me")
    ref = _new_snapshot_ref()
    store = SnapshotStore(tmp_path / "snapshots")
    store.root.mkdir(parents=True)
    (store.root / f"{ref}.snap").write_bytes(b"private snapshot")
    import app.agent.file_mutation.snapshot_store as snapshot_store_module

    monkeypatch.setattr(snapshot_store_module, "_DEFAULT_STORE", store)
    _terminal_run_with_snapshot(db, run_id="delete-run", conversation_id="delete-me", snapshot_ref=ref)

    assert db.delete_conversation("delete-me") is True
    assert not (store.root / f"{ref}.snap").exists()
    assert db.fetchone(
        "SELECT state FROM snapshot_ref_gc WHERE snapshot_ref = ?", (ref,)
    )[0] == "reclaimed"
    assert db.fetchone("SELECT 1 FROM agent_runs WHERE run_id = 'delete-run'") is None
    db.close()


def test_shared_snapshot_survives_until_last_run_reference_and_tombstone_blocks_reuse(
    tmp_path, monkeypatch
):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    db.create_conversation("first-chat", "First")
    db.create_conversation("second-chat", "Second")
    db.create_conversation("third-chat", "Third")
    ref = _new_snapshot_ref()
    store = SnapshotStore(tmp_path / "snapshots")
    store.root.mkdir(parents=True)
    snapshot_path = store.root / f"{ref}.snap"
    snapshot_path.write_bytes(b"shared private snapshot")
    import app.agent.file_mutation.snapshot_store as snapshot_store_module

    monkeypatch.setattr(snapshot_store_module, "_DEFAULT_STORE", store)
    _terminal_run_with_snapshot(db, run_id="first-run", conversation_id="first-chat", snapshot_ref=ref)
    _terminal_run_with_snapshot(db, run_id="second-run", conversation_id="second-chat", snapshot_ref=ref)

    assert db.delete_conversation("first-chat") is True
    assert snapshot_path.exists()
    assert db.fetchone("SELECT 1 FROM snapshot_ref_gc WHERE snapshot_ref = ?", (ref,)) is None

    assert db.delete_conversation("second-chat") is True
    assert not snapshot_path.exists()
    assert db.fetchone(
        "SELECT state FROM snapshot_ref_gc WHERE snapshot_ref = ?", (ref,)
    )[0] == "reclaimed"

    _terminal_run_with_snapshot(
        db,
        run_id="third-run",
        conversation_id="third-chat",
        snapshot_ref=_new_snapshot_ref(),
    )
    with pytest.raises(sqlite3.IntegrityError, match="snapshot reference is being reclaimed"):
        db.execute(
            "UPDATE run_operations SET snapshot_ref = ? WHERE run_id = ?",
            (ref, "third-run"),
        )
    db.conn.rollback()
    db.close()


def test_snapshot_gc_retries_deferred_unlink_without_opening_reference_race(tmp_path, monkeypatch):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    db.create_conversation("deferred-chat", "Deferred")
    db.create_conversation("new-chat", "New")
    ref = _new_snapshot_ref()
    store = SnapshotStore(tmp_path / "snapshots")
    store.root.mkdir(parents=True)
    snapshot_path = store.root / f"{ref}.snap"
    snapshot_path.write_bytes(b"retryable private snapshot")

    class DeferredStore(SnapshotStore):
        def reclaim_refs(self, snapshot_refs):
            return {
                "deleted_refs": set(),
                "missing_refs": set(),
                "skipped_refs": set(snapshot_refs),
                "snapshot_bytes_deleted": 0,
            }

    import app.agent.file_mutation.snapshot_store as snapshot_store_module

    monkeypatch.setattr(snapshot_store_module, "_DEFAULT_STORE", DeferredStore(store.root))
    _terminal_run_with_snapshot(
        db,
        run_id="deferred-run",
        conversation_id="deferred-chat",
        snapshot_ref=ref,
    )
    assert db.delete_conversation("deferred-chat") is True
    assert snapshot_path.exists()
    assert db.fetchone(
        "SELECT state FROM snapshot_ref_gc WHERE snapshot_ref = ?", (ref,)
    )[0] == "pending"

    _terminal_run_with_snapshot(
        db,
        run_id="new-run",
        conversation_id="new-chat",
        snapshot_ref=_new_snapshot_ref(),
    )
    with pytest.raises(sqlite3.IntegrityError, match="snapshot reference is being reclaimed"):
        db.execute(
            "UPDATE run_operations SET snapshot_ref = ? WHERE run_id = ?",
            (ref, "new-run"),
        )
    db.conn.rollback()

    retried = reclaim_pending_snapshot_refs(db, snapshot_refs={ref}, store=store)
    assert retried["snapshot_files_deleted"] == 1
    assert not snapshot_path.exists()
    assert db.fetchone(
        "SELECT state FROM snapshot_ref_gc WHERE snapshot_ref = ?", (ref,)
    )[0] == "reclaimed"
    db.close()


def test_delete_preserves_snapshot_for_recoverable_or_needs_review_run(tmp_path, monkeypatch):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    db.create_conversation("review-chat", "Review")
    ref = _new_snapshot_ref()
    store = SnapshotStore(tmp_path / "snapshots")
    store.root.mkdir(parents=True)
    snapshot_path = store.root / f"{ref}.snap"
    snapshot_path.write_bytes(b"recovery evidence")
    import app.agent.file_mutation.snapshot_store as snapshot_store_module

    monkeypatch.setattr(snapshot_store_module, "_DEFAULT_STORE", store)
    _terminal_run_with_snapshot(
        db,
        run_id="review-run",
        conversation_id="review-chat",
        snapshot_ref=ref,
        status="needs_review",
        operation_status="needs_review",
    )

    with pytest.raises(ConversationHasUnresolvedRunError):
        db.delete_conversation("review-chat")
    assert db.fetchone("SELECT 1 FROM conversations WHERE id = 'review-chat'") is not None
    assert db.fetchone("SELECT 1 FROM run_operations WHERE run_id = 'review-run'") is not None
    assert snapshot_path.exists()

    import app.api.routes.conversations as conversation_routes

    monkeypatch.setattr(conversation_routes, "peek_memory_manager", lambda: None)
    monkeypatch.setattr(conversation_routes, "delete_conversation_file", lambda value: db.delete_conversation(value))
    with pytest.raises(HTTPException) as response:
        asyncio.run(conversations.delete_conversation("review-chat"))
    assert response.value.status_code == 409
    db.close()


def test_periodic_cleanup_preserves_expired_snapshot_for_needs_review_run(tmp_path, monkeypatch):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    db.create_conversation("review-cleanup-chat", "Review cleanup")
    ref = _new_snapshot_ref()
    import app.agent.file_mutation.snapshot_store as snapshot_store_module

    monkeypatch.setattr(snapshot_store_module, "RUNTIME_DIR", tmp_path / "runtime")
    store = SnapshotStore()
    store.root.mkdir(parents=True)
    path = store.root / f"{ref}.snap"
    path.write_bytes(b"preserve unresolved recovery evidence")
    old = datetime.now(timezone.utc).timestamp() - 45 * 24 * 60 * 60
    os.utime(path, (old, old))
    monkeypatch.setattr(snapshot_store_module, "_DEFAULT_STORE", store)
    _terminal_run_with_snapshot(
        db,
        run_id="review-cleanup-run",
        conversation_id="review-cleanup-chat",
        snapshot_ref=ref,
        status="needs_review",
        operation_status="needs_review",
    )

    from app.agent.durable.recovery import _cleanup_file_mutation_snapshots

    report = _cleanup_file_mutation_snapshots(Journal(db))
    assert report["snapshot_files_deleted"] == 0
    assert path.exists()
    db.close()


def test_terminal_event_retention_is_bounded_and_leaves_recoverable_history_intact(tmp_path):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    journal = Journal(db)

    def make_run(run_id: str, conversation_id: str, *, terminal: bool):
        db.create_conversation(conversation_id, conversation_id)
        created = journal.create_run(
            run_id=run_id,
            conversation_id=conversation_id,
            execution_source="desktop",
            owner_instance_id=f"owner-{run_id}",
            turn_id=f"turn-{run_id}",
        )
        if terminal:
            db.execute(
                "UPDATE agent_runs SET status = 'completed', finished_at = ?, updated_at = ? WHERE run_id = ?",
                (datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), run_id),
            )
            db.commit()
        for index in range(6):
            with db.transaction() as conn:
                journal._append_event(
                    conn,
                    run_id,
                    "run.updated",
                    {"status": "completed" if terminal else "running", "revision": index + 2},
                    at=(datetime.now(timezone.utc) + timedelta(microseconds=index)).isoformat(),
                )
        return created

    first = make_run("terminal-a", "terminal-chat-a", terminal=True)
    second = make_run("terminal-b", "terminal-chat-b", terminal=True)
    active = make_run("active-run", "active-chat", terminal=False)
    active_last_seq = int(journal.snapshot(active["run_id"])["last_seq"])

    result = journal.enforce_terminal_event_retention(
        max_age_days=90,
        max_events_per_run=3,
        max_total_events=4,
    )

    assert int(result["run_events_deleted"]) >= 8
    assert db.fetchone(
        """SELECT COUNT(*) FROM run_events AS e JOIN agent_runs AS r ON r.run_id = e.run_id
           WHERE r.status IN ('completed', 'failed', 'cancelled')"""
    )[0] == 4
    assert db.fetchone("SELECT COUNT(*) FROM run_events WHERE run_id = ?", (active["run_id"],))[0] == active_last_seq
    assert journal.snapshot(first["run_id"])["last_seq"] == 7
    assert journal.snapshot(second["run_id"])["last_seq"] == 7
    with pytest.raises(EventsExpiredError):
        journal.read_events(first["run_id"], after_seq=0)
    assert journal.read_events(first["run_id"], after_seq=7)["events"] == []
    assert journal.read_events(active["run_id"], after_seq=0)["last_seq"] == active_last_seq
    db.close()


def test_event_age_retention_only_removes_terminal_run_events(tmp_path):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    journal = Journal(db)
    db.create_conversation("old-terminal-chat", "Old terminal")
    terminal = journal.create_run(
        run_id="old-terminal-run",
        conversation_id="old-terminal-chat",
        execution_source="desktop",
        owner_instance_id="owner-old-terminal",
        turn_id="turn-old-terminal",
    )
    old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    db.execute(
        "UPDATE agent_runs SET status = 'failed', finished_at = ?, updated_at = ? WHERE run_id = ?",
        (old, old, terminal["run_id"]),
    )
    db.commit()
    db.create_conversation("recent-active-chat", "Recent active")
    active = journal.create_run(
        run_id="recent-active-run",
        conversation_id="recent-active-chat",
        execution_source="desktop",
        owner_instance_id="owner-recent-active",
        turn_id="turn-recent-active",
    )

    result = journal.enforce_terminal_event_retention(max_age_days=90)

    assert result["run_events_deleted"] == 1
    assert db.fetchone("SELECT COUNT(*) FROM run_events WHERE run_id = ?", (terminal["run_id"],))[0] == 0
    assert db.fetchone("SELECT COUNT(*) FROM run_events WHERE run_id = ?", (active["run_id"],))[0] == 1
    with pytest.raises(EventsExpiredError):
        journal.read_events(terminal["run_id"], after_seq=0)
    db.close()


def test_global_event_retention_removes_sequence_prefix_despite_clock_skew(tmp_path):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    journal = Journal(db)
    db.create_conversation("clock-skew-chat", "Clock skew")
    run = journal.create_run(
        run_id="clock-skew-run",
        conversation_id="clock-skew-chat",
        execution_source="desktop",
        owner_instance_id="owner-clock-skew",
        turn_id="turn-clock-skew",
    )
    now = datetime.now(timezone.utc)
    db.execute(
        "UPDATE agent_runs SET status = 'completed', finished_at = ?, updated_at = ? WHERE run_id = ?",
        (now.isoformat(), now.isoformat(), run["run_id"]),
    )
    db.commit()
    skewed_event_times = [20, -20, 5, -30, 2]
    for index, seconds in enumerate(skewed_event_times, start=1):
        with db.transaction() as conn:
            journal._append_event(
                conn,
                run["run_id"],
                "run.updated",
                {"status": "completed", "revision": index + 1},
                at=(now + timedelta(days=seconds)).isoformat(),
            )

    journal.enforce_terminal_event_retention(
        now=now + timedelta(days=1),
        max_age_days=90,
        max_events_per_run=100,
        max_total_events=3,
    )

    retained = db.fetchall(
        "SELECT seq FROM run_events WHERE run_id = ? ORDER BY seq", (run["run_id"],)
    )
    assert [int(row["seq"]) for row in retained] == [4, 5, 6]
    assert journal.read_events(run["run_id"], after_seq=3)["next_seq"] == 6
    with pytest.raises(EventsExpiredError):
        journal.read_events(run["run_id"], after_seq=0)
    db.close()
