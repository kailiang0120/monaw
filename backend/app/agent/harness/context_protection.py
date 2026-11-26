"""Select exact failure context independently of generated conversation summaries."""

import json

PROTECTED_CONTEXT = "_context_protected"
MAX_RESTORED_TASK_MESSAGES = 1000


def protected_failure_ranges(db, conversation_id: str) -> list[tuple[int, int]]:
    """Retain failed turns until their task is completed or explicitly replaced."""
    row = db.fetchone(
        "SELECT state_json FROM task_progress WHERE conversation_id=? AND active=1",
        (conversation_id,),
    )
    task = json.loads(row["state_json"]) if row else {}
    floor = int(task.get("history_start_id", 0))
    latest = db.fetchone(
        "SELECT id, status FROM messages WHERE conversation_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",
        (conversation_id,),
    )
    if latest is None or (
        latest["status"] == "complete" and task.get("status") not in {"working", "blocked", "waiting_user"}
    ):
        return []
    # Without task state, retain the failure chain since the last successful
    # reply. Current tasks instead use their explicit history boundary.
    if not task:
        previous_success = db.fetchone(
            """SELECT COALESCE(MAX(id), 0) AS id FROM messages WHERE conversation_id=?
               AND role='assistant' AND status='complete' AND content!=''""",
            (conversation_id,),
        )
        floor = int(previous_success["id"])
    failures = db.fetchall(
        """SELECT failure.id AS end_id, COALESCE(
               (SELECT MAX(checkpoint.assistant_message_id) FROM turn_checkpoints checkpoint
                WHERE checkpoint.conversation_id=failure.conversation_id
                  AND checkpoint.assistant_message_id<failure.id),
               (SELECT MAX(previous.id) FROM messages previous
                WHERE previous.conversation_id=failure.conversation_id
                  AND previous.role='assistant' AND previous.content!='' AND previous.id<failure.id),
               0
           ) AS previous_id
           FROM messages failure WHERE failure.conversation_id=? AND failure.role='assistant'
             AND failure.status IN ('error', 'paused') AND failure.id>? ORDER BY failure.id""",
        (conversation_id, floor),
    )
    return [(max(floor, int(row["previous_id"])) + 1, int(row["end_id"])) for row in failures]
