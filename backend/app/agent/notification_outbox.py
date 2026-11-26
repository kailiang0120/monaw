"""Durable delivery state, separate from scheduled agent execution."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

MAX_DELIVERY_ATTEMPTS = 5


def enqueue_notification(connection, run_id: int, task_id: str, payload: dict, now: str) -> None:
    """Persist one notification in the same transaction as the execution result."""
    connection.execute(
        """INSERT OR IGNORE INTO scheduled_notifications
           (run_id, task_id, payload, next_attempt_at) VALUES (?, ?, ?, ?)""",
        (run_id, task_id, json.dumps(payload, ensure_ascii=False), now),
    )


def claim_notification(database, owner: str, *, run_id: int | None = None) -> dict | None:
    """Lease one due delivery without claiming or replaying an agent run."""
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    with database.transaction() as connection:
        connection.execute(
            """UPDATE scheduled_notifications SET status = 'failed', lease_owner = '',
               lease_expires_at = '', last_error = 'delivery_interrupted'
               WHERE status = 'sending' AND lease_expires_at <= ? AND attempts >= ?""",
            (now_iso, MAX_DELIVERY_ATTEMPTS),
        )
        scope = " AND run_id = ?" if run_id is not None else ""
        params = [now_iso, now_iso, MAX_DELIVERY_ATTEMPTS]
        if run_id is not None:
            params.append(run_id)
        row = connection.execute(
            """SELECT * FROM scheduled_notifications
               WHERE ((status = 'pending' AND next_attempt_at <= ?)
                  OR (status = 'sending' AND lease_expires_at <= ?)) AND attempts < ?"""
            + scope + " ORDER BY next_attempt_at, run_id LIMIT 1",
            params,
        ).fetchone()
        if row is None:
            return None
        connection.execute(
            """UPDATE scheduled_notifications SET status = 'sending', attempts = attempts + 1,
               lease_owner = ?, lease_expires_at = ? WHERE run_id = ?""",
            (owner, (now + timedelta(minutes=5)).isoformat(), row["run_id"]),
        )
        return {**dict(row), "attempts": row["attempts"] + 1, "payload": json.loads(row["payload"])}


def record_delivered_chunk(database, run_id: int, owner: str, next_chunk: int) -> None:
    """Do not resend chunks already acknowledged by Telegram on a later retry."""
    with database.transaction() as connection:
        result = connection.execute(
            """UPDATE scheduled_notifications SET next_chunk = ?
               WHERE run_id = ? AND lease_owner = ? AND status = 'sending'""",
            (next_chunk, run_id, owner),
        )
        if result.rowcount != 1:
            raise RuntimeError("notification_lease_lost")


def finish_notification(database, delivery: dict, owner: str, *, error: str = "", blocked: bool = False) -> str:
    """Record delivery success or bounded backoff without storing transport secrets."""
    now = datetime.now(timezone.utc)
    status = "sent"
    if error:
        status = "blocked" if blocked else "failed" if delivery["attempts"] >= MAX_DELIVERY_ATTEMPTS else "pending"
    next_attempt = now + timedelta(seconds=min(3600, 30 * 2 ** (delivery["attempts"] - 1)))
    with database.transaction() as connection:
        connection.execute(
            """UPDATE scheduled_notifications SET status = ?, last_error = ?, next_attempt_at = ?,
               sent_at = ?, lease_owner = '', lease_expires_at = ''
               WHERE run_id = ? AND lease_owner = ? AND status = 'sending'""",
            (status, error, next_attempt.isoformat(), now.isoformat() if status == "sent" else "",
             delivery["run_id"], owner),
        )
    return status
