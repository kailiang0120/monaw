from __future__ import annotations

import asyncio

from app.agent.database import Database
from app.agent.long_term_memory import LongTermMemory


def _store(tmp_path) -> LongTermMemory:
    db = Database(tmp_path / "agent.db")
    db.init_db()
    db.create_conversation("conv-reconcile", "Reconcile")
    return LongTermMemory(db=db, memory_root=tmp_path / "memory")


def test_memory_curator_updates_conflicting_preference_at_session_close(tmp_path):
    store = _store(tmp_path)
    store._db.add_message("conv-reconcile", "user", "I prefer Python.")
    store._db.add_message("conv-reconcile", "assistant", "Noted.")
    store._db.add_message("conv-reconcile", "user", "I prefer TypeScript.")
    store._db.add_message("conv-reconcile", "assistant", "Noted.")

    result = asyncio.run(store.curate_session("conv-reconcile"))
    memories = store.list_memories(status="active")
    audit = store.audit_log(conversation_id="conv-reconcile", limit=20)

    assert result["processed"] >= 2
    assert result["added"] >= 1
    assert any("python" in memory["content"].lower() for memory in memories)
    assert any("typescript" in memory["content"].lower() for memory in memories)
    assert any(row["action"] == "CURATE" for row in audit)
