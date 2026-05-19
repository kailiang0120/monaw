from __future__ import annotations

from app.agent.database import Database


def test_archive_old_messages_preserves_and_restores_rows(tmp_path):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    db.create_conversation("conv-archive", "Archive")
    message_ids = [
        db.add_message(
            "conv-archive",
            "assistant" if index == 0 else "user",
            f"message {index}",
            response_duration_ms=1234 if index == 0 else None,
            attachments_json='[{"name":"image.png"}]' if index == 0 else "",
        )
        for index in range(60)
    ]

    archived = db.archive_old_messages("conv-archive", keep_recent=10)

    assert archived == 50
    assert db.count_messages("conv-archive") == 10
    assert db.count_archived_messages("conv-archive") == 50

    restored = db.restore_message(message_ids[0])

    assert restored is not None
    assert restored["content"] == "message 0"
    assert restored["response_duration_ms"] == 1234
    assert restored["attachments_json"] == '[{"name":"image.png"}]'
    assert db.count_messages("conv-archive") == 11
    assert db.count_archived_messages("conv-archive") == 49
