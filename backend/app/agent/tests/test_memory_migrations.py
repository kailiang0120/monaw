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
    assert "memory_audit" not in tables
    assert "memory_candidates" in tables
    assert "memory_profile_fields" in tables
    assert "memory_episodes" in tables
    assert "memory_checkpoints" in tables
    assert "conversation_compactions" in tables
    assert "memory_embeddings" not in tables
    assert "memory_fts" not in tables
    assert versions == [5, 6, 7, 8, 9, 10]
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
    }.issubset(indexes)
