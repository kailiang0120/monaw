from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path

import pytest

from app.agent.database import Database
from app.agent.migrations import runner as migration_runner


def _v17_migration_directory(tmp_path: Path) -> Path:
    """Copy the repository's real migration history, stopping at schema v17."""
    source = Path(migration_runner.__file__).resolve().parent
    destination = tmp_path / "migrations-v17"
    destination.mkdir()
    for path in source.glob("*.sql"):
        match = re.match(r"^(\d{4})_", path.name)
        if match and int(match.group(1)) <= 17:
            shutil.copyfile(path, destination / path.name)
    return destination


def test_v17_database_upgrades_in_place_and_retains_existing_rows(tmp_path, monkeypatch):
    database_path = tmp_path / "agent.db"
    v17_migrations = _v17_migration_directory(tmp_path)
    latest_migrations = migration_runner._migration_dir
    monkeypatch.setattr(migration_runner, "_migration_dir", lambda: v17_migrations)

    old_db = Database(database_path)
    old_db.init_db()
    assert [row["version"] for row in old_db.fetchall("SELECT version FROM schema_migrations ORDER BY version")] == list(
        range(5, 18)
    )

    old_db.create_conversation("legacy-chat", "Legacy chat", workspace_dir="C:/work/legacy")
    message = old_db.execute(
        "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        ("legacy-chat", "user", "keep this message", "2026-10-30T00:00:00Z"),
    )
    message_id = int(message.lastrowid)
    old_db.execute(
        "INSERT INTO turn_checkpoints (turn_id, conversation_id, user_message_ids) VALUES (?, ?, ?)",
        ("legacy-turn", "legacy-chat", json.dumps([message_id])),
    )
    old_db.commit()
    old_db.close()

    # Reopening with the current runner applies only the new forward migrations.
    monkeypatch.setattr(migration_runner, "_migration_dir", latest_migrations)
    upgraded = Database(database_path)
    upgraded.init_db()

    assert upgraded.fetchone("SELECT workspace_dir FROM conversations WHERE id = 'legacy-chat'")[0] == "C:/work/legacy"
    assert upgraded.fetchone("SELECT content FROM messages WHERE id = ?", (message_id,))[0] == "keep this message"
    assert upgraded.fetchone("SELECT user_message_ids FROM turn_checkpoints WHERE turn_id = 'legacy-turn'")[0] == json.dumps(
        [message_id]
    )
    versions = [row["version"] for row in upgraded.fetchall("SELECT version FROM schema_migrations ORDER BY version")]
    assert versions[:13] == list(range(5, 18))
    assert versions[-5:] == [18, 19, 20, 21, 22]
    upgraded.close()


def test_durable_tables_enforce_constraints_and_cascade_on_conversation_delete(tmp_path):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    db.create_conversation("durable-chat", "Durable chat", workspace_dir="C:/work")
    now = "2026-11-03T00:00:00Z"

    db.execute(
        """
        INSERT INTO agent_runs (
            run_id, conversation_id, workspace_dir, execution_source,
            attempt, revision, last_seq, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("run-1", "durable-chat", "C:/work", "desktop", 1, 0, 0, "running", now, now),
    )
    db.execute(
        "INSERT INTO run_attempts (run_id, attempt, turn_id, created_at) VALUES (?, ?, ?, ?)",
        ("run-1", 1, "turn-run-1", now),
    )
    db.execute(
        """
        INSERT INTO run_operations (
            operation_id, run_id, attempt, sequence_no, operation_kind,
            tool_name,
            effect_class, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("op-1", "run-1", 1, 1, "file_mutation", "write_file", "verifiable_write", "prepared", now, now),
    )
    db.execute(
        """
        INSERT INTO run_operations (
            operation_id, run_id, attempt, sequence_no, operation_kind,
            tool_name, effect_class, status, related_operation_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("op-2", "run-1", 1, 2, "undo", "undo_file", "verifiable_write", "prepared", "op-1", now, now),
    )
    db.execute(
        "INSERT INTO run_events (run_id, seq, event_type, created_at, payload_json) VALUES (?, ?, ?, ?, ?)",
        ("run-1", 1, "run.created", now, "{}"),
    )
    db.commit()

    for table, key, value in (
        ("agent_runs", "run_id", "run-1"),
        ("run_operations", "operation_id", "op-1"),
        ("run_operations", "operation_id", "op-2"),
        ("run_events", "run_id", "run-1"),
        ("run_attempts", "run_id", "run-1"),
    ):
        assert db.fetchone(f"SELECT 1 FROM {table} WHERE {key} = ?", (value,)) is not None

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO agent_runs (run_id, conversation_id, execution_source, attempt, revision, last_seq, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("bad-attempt", "durable-chat", "desktop", 0, 0, 0, "running", now, now),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO agent_runs (run_id, conversation_id, execution_source, attempt, revision, last_seq, status, created_at, updated_at) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("durable-chat", "desktop", 1, 0, 0, "running", now, now),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO run_events (run_id, seq, event_type, created_at, payload_json) VALUES (?, ?, ?, ?, ?)",
            ("run-1", 1, "duplicate", now, "{}"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO run_operations (operation_id, run_id, attempt, sequence_no, operation_kind, tool_name, effect_class, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("op-duplicate", "run-1", 1, 1, "file_mutation", "write_file", "verifiable_write", "prepared", now, now),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO run_operations (operation_id, run_id, attempt, sequence_no, operation_kind, tool_name, effect_class, status, created_at, updated_at) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("run-1", 1, 2, "file_mutation", "write_file", "verifiable_write", "prepared", now, now),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO run_operations (operation_id, run_id, attempt, sequence_no, operation_kind, tool_name, effect_class, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("op-missing-attempt", "run-1", 2, 3, "file_mutation", "write_file", "verifiable_write", "prepared", now, now),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO run_events (run_id, seq, event_type, created_at, payload_json) VALUES (?, ?, ?, ?, ?)",
            ("missing-run", 1, "run.created", now, "{}"),
        )
    db.conn.rollback()

    assert db.fetchone("SELECT 1 FROM agent_runs WHERE run_id = 'run-1'") is not None
    assert db.fetchone("SELECT 1 FROM run_operations WHERE operation_id = 'op-1'") is not None
    assert db.fetchone("SELECT 1 FROM run_operations WHERE operation_id = 'op-2' AND related_operation_id = 'op-1'") is not None
    assert db.fetchone("SELECT 1 FROM run_events WHERE run_id = 'run-1' AND seq = 1") is not None
    assert db.fetchone("SELECT 1 FROM run_attempts WHERE run_id = 'run-1' AND attempt = 1") is not None

    assert db.delete_conversation("durable-chat") is True
    for table in ("agent_runs", "run_operations", "run_events", "run_attempts"):
        assert db.fetchone(f"SELECT 1 FROM {table} WHERE run_id = 'run-1'") is None
    db.close()


def test_turn_checkpoint_retry_is_deduplicated_and_bound_to_its_conversation(tmp_path):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    db.create_conversation("checkpoint-chat", "Checkpoint chat")
    db.create_conversation("other-chat", "Other chat")
    first_messages = [{"role": "user", "content": "first request"}, {"role": "assistant", "content": "done"}]

    first_ids, inserted = db.persist_turn_messages("turn-once", "checkpoint-chat", first_messages)
    retry_ids, inserted_on_retry = db.persist_turn_messages(
        "turn-once", "checkpoint-chat", [{"role": "user", "content": "different retry payload"}]
    )

    assert inserted is True
    assert inserted_on_retry is False
    assert retry_ids == first_ids
    assert len(db.get_messages("checkpoint-chat")) == 2
    with pytest.raises(ValueError, match="another conversation"):
        db.persist_turn_messages("turn-once", "other-chat", [{"role": "user", "content": "must not attach"}])
    assert db.get_messages("other-chat") == []
    db.close()
