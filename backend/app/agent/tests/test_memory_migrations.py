from __future__ import annotations

from app.agent.database import Database


def test_fresh_database_does_not_create_sqlite_memory_schema(tmp_path):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    db.init_db()

    tables = {
        row["name"]
        for row in db.fetchall("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
    }
    versions = [
        row["version"]
        for row in db.fetchall("SELECT version FROM schema_migrations ORDER BY version")
    ]
    indexes = {
        row["name"]
        for row in db.fetchall("SELECT name FROM sqlite_master WHERE type = 'index'")
    }

    assert "messages_archive" in tables
    assert "memories" not in tables
    assert "memory_audit" in tables
    assert "memory_audit_counters" in tables
    assert "memory_candidates" in tables
    assert "memory_profile_fields" not in tables
    assert "memory_episodes" in tables
    assert "memory_checkpoints" in tables
    assert "conversation_compactions" in tables
    assert "memory_embeddings" not in tables
    assert "memory_fts" not in tables
    assert versions == [5, 6, 7, 8, 9, 10, 11, 12]
    assert {
        "idx_conversations_updated",
        "idx_tool_calls_conversation",
        "idx_tool_outcomes_conversation",
        "idx_plan_steps_conversation_status",
        "idx_scheduled_tasks_created",
        "idx_scheduled_task_runs_status",
        "idx_memory_candidates_status_updated",
        "idx_memory_checkpoints_status_updated",
        "idx_memory_episodes_updated",
        "idx_memory_audit_created",
        "idx_memory_audit_conversation_created",
    }.issubset(indexes)


def test_memory_migration_adds_deletion_cascade_and_trims_operational_layers(tmp_path):
    db = Database(tmp_path / "agent.db")
    db.init_db()

    archive_columns = {
        row["name"] for row in db.fetchall("PRAGMA table_info(messages_archive)")
    }
    episode_columns = {
        row["name"] for row in db.fetchall("PRAGMA table_info(memory_episodes)")
    }
    checkpoint_columns = {
        row["name"] for row in db.fetchall("PRAGMA table_info(memory_checkpoints)")
    }

    assert "conversation_id" in archive_columns
    assert "browser_url" not in checkpoint_columns
    assert "decisions_json" not in episode_columns
    foreign_keys = db.fetchall("PRAGMA foreign_key_list(messages_archive)")
    assert any(row["table"] == "conversations" and row["on_delete"] == "CASCADE" for row in foreign_keys)

    db.create_conversation("conv-cascade", "Cascade")
    db.execute(
        "INSERT INTO messages_archive (id, conversation_id, role, content, status, created_at, archived_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1, "conv-cascade", "user", "archived", "complete", "2026-01-01", "2026-01-02"),
    )
    db.commit()
    assert db.delete_conversation("conv-cascade") is True
    assert db.fetchone("SELECT id FROM messages_archive WHERE id = 1") is None
