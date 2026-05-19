"""Session-close memory consolidation and archive helpers."""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from app.agent.database import Database, get_db
from app.agent.long_term_memory import LongTermMemory, get_long_term_memory


async def close_session_async(
    conversation_id: str,
    *,
    store: LongTermMemory | None = None,
    db: Database | None = None,
    archive_keep_recent: int = 50,
    reflection_threshold_messages: int = 50,
) -> dict[str, Any]:
    """Curate durable Markdown memory and move old hot messages into cold archive."""
    active_db = db or get_db()
    active_store = store or get_long_term_memory()
    before_hot = active_db.count_messages(conversation_id)
    reconciliation = await active_store.curate_session(conversation_id)
    maintenance = active_store.maintain_memory_health(
        cooldown_hours=active_store.maintenance_cooldown_hours
    )
    archived = active_db.archive_old_messages(
        conversation_id,
        keep_recent=archive_keep_recent,
    )
    reflection = None
    if before_hot >= reflection_threshold_messages:
        reflection = active_store.remember(
            (
                f"Session {conversation_id} produced a durable conversation "
                f"summary covering {before_hot} messages."
            ),
            category="workflow",
            confidence=0.8,
            review_state="new",
            importance=6,
            kind="reflection",
            source="session_close",
            source_conversation_id=conversation_id,
        )
    return {
        "conversation_id": conversation_id,
        "reconciliation": reconciliation,
        "archived_messages": archived,
        "maintenance": maintenance,
        "hot_messages_before": before_hot,
        "hot_messages_after": active_db.count_messages(conversation_id),
        "reflection_id": reflection["id"] if reflection else None,
    }


def close_session(
    conversation_id: str,
    *,
    store: LongTermMemory | None = None,
    db: Database | None = None,
    archive_keep_recent: int = 50,
    reflection_threshold_messages: int = 50,
) -> dict[str, Any]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            close_session_async(
                conversation_id,
                store=store,
                db=db,
                archive_keep_recent=archive_keep_recent,
                reflection_threshold_messages=reflection_threshold_messages,
            )
        )
    active_store = store or get_long_term_memory()
    return active_store.reconcile_session(conversation_id)


def run_for_all_conversations(
    *,
    store: LongTermMemory | None = None,
    db: Database | None = None,
    archive_keep_recent: int = 50,
) -> list[dict[str, Any]]:
    active_db = db or get_db()
    active_store = store or get_long_term_memory()
    results: list[dict[str, Any]] = []
    for conversation in active_db.list_conversations():
        results.append(
            close_session(
                conversation["id"],
                store=active_store,
                db=active_db,
                archive_keep_recent=archive_keep_recent,
            )
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Consolidate agent memory sessions.")
    parser.add_argument("--conversation-id", default="", help="Conversation to close.")
    parser.add_argument("--keep-recent", type=int, default=50, help="Hot messages to keep.")
    args = parser.parse_args()
    if args.conversation_id:
        result = close_session(args.conversation_id, archive_keep_recent=args.keep_recent)
        print(result)
    else:
        for result in run_for_all_conversations(archive_keep_recent=args.keep_recent):
            print(result)


if __name__ == "__main__":
    main()
