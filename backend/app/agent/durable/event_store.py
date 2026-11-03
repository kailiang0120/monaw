"""Read-only durable event replay with cursor expiry detection."""

from __future__ import annotations

import json
from typing import Any

from app.agent.database import Database
from app.agent.durable.models import (
    EVENT_TYPES,
    EventPageDTO,
    EventsExpiredError,
    InvalidCursorError,
    RunNotFoundError,
    sanitize_event_payload,
)


class EventStore:
    """Reads bounded public DTOs from the durable run event log."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def read_events(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        limit: int = 100,
    ) -> EventPageDTO:
        if isinstance(after_seq, bool) or not isinstance(after_seq, int) or after_seq < 0:
            raise InvalidCursorError("after_seq must be a nonnegative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, 500)

        with self.db.transaction(immediate=False) as conn:
            run = conn.execute(
                "SELECT last_seq FROM agent_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise RunNotFoundError(run_id)
            last_seq = int(run["last_seq"])
            if after_seq > last_seq:
                raise InvalidCursorError("after_seq is beyond the latest event")

            first = conn.execute(
                "SELECT MIN(seq) AS seq FROM run_events WHERE run_id = ?", (run_id,)
            ).fetchone()
            first_seq = int(first["seq"]) if first and first["seq"] is not None else None
            if after_seq < last_seq and (
                first_seq is None or first_seq > after_seq + 1
            ):
                raise EventsExpiredError("event cursor predates retained events")

            rows = conn.execute(
                """SELECT seq, event_type, payload_json, created_at
                   FROM run_events
                   WHERE run_id = ? AND seq > ?
                   ORDER BY seq ASC LIMIT ?""",
                (run_id, after_seq, limit),
            ).fetchall()

            events: list[dict[str, Any]] = []
            expected_seq = after_seq + 1
            for row in rows:
                seq = int(row["seq"])
                if seq != expected_seq:
                    raise EventsExpiredError("event cursor crosses a retention gap")
                event_type = str(row["event_type"])
                if event_type not in EVENT_TYPES:
                    # Do not return an unknown stored event type to clients.
                    raise ValueError("unsupported stored event type")
                try:
                    payload = json.loads(row["payload_json"])
                except (TypeError, json.JSONDecodeError):
                    payload = {}
                events.append(
                    {
                        "run_id": run_id,
                        "seq": seq,
                        "type": event_type,
                        "at": str(row["created_at"]),
                        "payload": sanitize_event_payload(event_type, payload),
                    }
                )
                expected_seq += 1

            if not rows and after_seq < last_seq:
                raise EventsExpiredError("event cursor predates retained events")

            next_seq = int(events[-1]["seq"]) if events else after_seq
            return {
                "run_id": run_id,
                "events": events,
                "next_seq": next_seq,
                "last_seq": last_seq,
            }
