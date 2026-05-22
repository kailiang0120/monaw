from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.agent.database import Database
from app.agent.long_term_memory import LongTermMemory
from app.agent.memory_consolidation import close_session


def _store(tmp_path) -> tuple[Database, LongTermMemory]:
    db = Database(tmp_path / "agent.db")
    db.init_db()
    return db, LongTermMemory(db=db, memory_root=tmp_path / "memory")


def test_close_session_archives_hot_messages_and_creates_markdown_reflection(tmp_path):
    db, store = _store(tmp_path)
    db.create_conversation("conv-close", "Close")
    for index in range(60):
        db.add_message("conv-close", "user", f"I prefer progress update {index}.")

    result = close_session(
        "conv-close",
        store=store,
        db=db,
        archive_keep_recent=10,
        reflection_threshold_messages=50,
    )

    reflections = store.list_memories(category="reflection", status="active", limit=10)

    assert result["archived_messages"] == 50
    assert set(result["maintenance"]) == {"archived_stale", "merged", "promoted", "demoted", "skipped"}
    assert result["hot_messages_after"] == 10
    assert db.count_archived_messages("conv-close") == 50
    assert len(reflections) >= 1
    assert (tmp_path / "memory" / "short-term" / "session-conv-close.md").exists()
    episodes = store.list_episodes(conversation_id="conv-close")
    assert len(episodes) == 1
    assert "Goal:" in episodes[0]["summary"]


def test_memory_health_archives_tunes_and_merges_markdown_memories(tmp_path):
    _, store = _store(tmp_path)
    stale = store.remember("Low value stale workflow note.", category="workflow", importance=3)
    popular = store.remember("The user prefers PowerShell for repo commands.", category="preference", importance=8)
    unused = store.remember("Old unused project detail.", category="project", importance=5)
    duplicate = store.remember("The user prefers PowerShell for repository commands.", category="preference", importance=7)

    assert stale and popular and unused and duplicate
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    store.update(stale["id"], importance=3)
    stale_record = store._find(stale["id"])
    assert stale_record is not None
    stale_record["last_used_at"] = old
    stale_record["updated_at"] = old
    store._write_record(stale_record)

    popular_record = store._find(popular["id"])
    assert popular_record is not None
    popular_record["use_count"] = 20
    store._write_record(popular_record)

    unused_record = store._find(unused["id"])
    assert unused_record is not None
    unused_record["created_at"] = old
    unused_record["updated_at"] = old
    store._write_record(unused_record)

    result = store.maintain_memory_health(cooldown_hours=0, merge_threshold=0.6)
    memories = store.list_memories(status="", limit=20)
    by_id = {memory["id"]: memory for memory in memories}

    assert result["archived_stale"] == 1
    assert result["promoted"] >= 1
    assert result["demoted"] == 1
    assert result["merged"] >= 1
    assert by_id[stale["id"]]["status"] == "archived"
    assert by_id[unused["id"]]["importance"] == 4
    assert by_id[duplicate["id"]]["status"] == "archived"


def test_memory_health_uses_cooldown_marker(tmp_path):
    _, store = _store(tmp_path)
    fresh = store.remember("Fresh low value note.", category="workflow", importance=3)
    old_never_used = store.remember("Old low value note.", category="workflow", importance=3)

    assert fresh and old_never_used
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    old_record = store._find(old_never_used["id"])
    assert old_record is not None
    old_record["created_at"] = old
    old_record["updated_at"] = old
    old_record["last_used_at"] = ""
    store._write_record(old_record)

    first = store.maintain_memory_health(cooldown_hours=24)
    second = store.maintain_memory_health(cooldown_hours=24)
    memories = store.list_memories(status="", limit=20)
    by_id = {memory["id"]: memory for memory in memories}

    assert first["archived_stale"] == 1
    assert first["skipped"] == 0
    assert second == {"archived_stale": 0, "merged": 0, "promoted": 0, "demoted": 0, "skipped": 1}
    assert by_id[fresh["id"]]["status"] == "active"
    assert by_id[old_never_used["id"]]["status"] == "archived"
