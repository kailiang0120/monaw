"""Approval broker – ticket lifecycle for sensitive action confirmation.

Tickets are persisted as JSONL for durability across restarts. An in-memory
index tracks pending tickets for fast lookup.

Resume signaling:
  The react loop calls register_pending_resume(ticket_id) to create an
  asyncio.Event it will wait on.  The approve/reject endpoints call
  signal_resume so the loop wakes up with the decision and replays the
  tool instead of continuing blindly.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.agent.runtime_paths import RUNTIME_DIR
from app.agent.run_context import current_conversation_id

_APPROVALS_DIR = RUNTIME_DIR / "approvals"
_APPROVALS_DIR.mkdir(parents=True, exist_ok=True)

_TICKETS_FILE = _APPROVALS_DIR / "tickets.jsonl"
_APPROVAL_LOG = _APPROVALS_DIR.parent / "policy" / "approval_log.md"

# H5: Validate paths are within expected runtime root
_RUNTIME_ROOT = _APPROVALS_DIR.resolve().parents[0]
assert _APPROVALS_DIR.resolve().is_relative_to(_RUNTIME_ROOT), (
    f"Approval dir {_APPROVALS_DIR} escapes expected runtime root"
)


class TicketStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    APPLIED = "applied"
    FAILED = "failed"


class ApprovalTicket(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    conversation_id: str = ""
    action_type: str = ""
    tool_name: str = ""
    target_path: str = ""
    target_app: str = ""
    risk_level: str = "medium"
    reason: str = ""
    action_description: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_hash: str = ""
    status: TicketStatus = TicketStatus.PENDING
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    resolved_at: str = ""
    resolved_by: str = ""
    execution_result: str = ""

    def compute_hash(self) -> str:
        raw = json.dumps(self.payload, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── Persistence ───────────────────────────────────────────────────────────────

# C1: Single lock guards all mutable module-level state (ticket dicts + resume maps).
# The same lock is held during file I/O so a reader never sees an inconsistent view.
_state_lock: threading.RLock = threading.RLock()

_pending_index: dict[str, ApprovalTicket] = {}
_all_tickets: dict[str, ApprovalTicket] = {}


def _load_tickets_from_disk() -> None:
    """Reload all tickets from JSONL on startup."""
    _pending_index.clear()
    _all_tickets.clear()
    if not _TICKETS_FILE.exists():
        return
    with open(_TICKETS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                ticket = ApprovalTicket.model_validate(data)
                _all_tickets[ticket.id] = ticket
                if ticket.status == TicketStatus.PENDING:
                    _pending_index[ticket.id] = ticket
            except Exception:
                continue


def _ensure_approvals_dir() -> None:
    """Make sure the approvals directory exists.

    The directory is created once at module import, but something (a test, a
    manual cleanup, a disk cleanup job) may have removed it since then. Every
    write goes through here so approve/reject don't blow up with ENOENT.
    """
    _APPROVALS_DIR.mkdir(parents=True, exist_ok=True)


def _append_ticket(ticket: ApprovalTicket) -> None:
    _ensure_approvals_dir()
    with open(_TICKETS_FILE, "a", encoding="utf-8") as f:
        f.write(ticket.model_dump_json() + "\n")


def _rewrite_tickets() -> None:
    """Rewrite full JSONL from in-memory state (after status changes)."""
    _ensure_approvals_dir()
    with open(_TICKETS_FILE, "w", encoding="utf-8") as f:
        for t in _all_tickets.values():
            f.write(t.model_dump_json() + "\n")


def _log_approval_md(action: str, ticket: ApprovalTicket) -> None:
    """Append entry to approval_log.md."""
    _APPROVAL_LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = (
        f"- {ts} | **{action}** | ticket={ticket.id} | "
        f"tool={ticket.tool_name} | target={ticket.target_path or ticket.target_app} | "
        f"status={ticket.status.value}\n"
    )
    if not _APPROVAL_LOG.exists():
        header = "# Approval Log\n\n> Timestamped record of all approval decisions.\n\n"
        _APPROVAL_LOG.write_text(header + entry, encoding="utf-8")
    else:
        with open(_APPROVAL_LOG, "a", encoding="utf-8") as f:
            f.write(entry)


# ── Resume signaling (set by react loop; signaled by approve/reject endpoints) ─

_resume_events: dict[str, asyncio.Event] = {}
_resume_decisions: dict[str, str] = {}  # "approved" | "rejected"


def register_pending_resume(ticket_id: str) -> asyncio.Event:
    """Register an asyncio.Event the react loop will wait on. Call from async context."""
    event = asyncio.Event()
    with _state_lock:
        _resume_events[ticket_id] = event
        if ticket_id in _resume_decisions:
            event.set()
    return event


def signal_resume(ticket_id: str, decision: str) -> None:
    """Signal the waiting react loop. Call from async context after approve/reject."""
    with _state_lock:
        _resume_decisions[ticket_id] = decision
        event = _resume_events.get(ticket_id)
    if event is not None:
        event.set()


def get_resume_decision(ticket_id: str) -> str | None:
    """Retrieve and remove the stored decision for a ticket."""
    with _state_lock:
        return _resume_decisions.pop(ticket_id, None)


def cleanup_resume(ticket_id: str) -> None:
    """Clean up resume state after the loop has processed the resolution."""
    with _state_lock:
        _resume_events.pop(ticket_id, None)
        _resume_decisions.pop(ticket_id, None)


# ── Boot ──────────────────────────────────────────────────────────────────────

_load_tickets_from_disk()


# ── Public API ────────────────────────────────────────────────────────────────


def create_ticket(
    *,
    conversation_id: str = "",
    action_type: str = "",
    tool_name: str = "",
    target_path: str = "",
    target_app: str = "",
    risk_level: str = "medium",
    reason: str = "",
    action_description: str = "",
    payload: dict[str, Any] | None = None,
) -> ApprovalTicket:
    """Create and persist a new pending approval ticket."""
    ticket = ApprovalTicket(
        conversation_id=conversation_id or current_conversation_id(),
        action_type=action_type,
        tool_name=tool_name,
        target_path=target_path,
        target_app=target_app,
        risk_level=risk_level,
        reason=reason,
        action_description=action_description,
        payload=payload or {},
    )
    ticket.payload_hash = ticket.compute_hash()
    with _state_lock:
        _all_tickets[ticket.id] = ticket
        _pending_index[ticket.id] = ticket
        _append_ticket(ticket)
    _log_approval_md("CREATED", ticket)
    return ticket


def get_ticket(ticket_id: str) -> ApprovalTicket | None:
    return _all_tickets.get(ticket_id)


def get_pending_tickets(conversation_id: str = "") -> list[ApprovalTicket]:
    """Return all pending tickets, optionally filtered by conversation."""
    tickets = list(_pending_index.values())
    if conversation_id:
        tickets = [t for t in tickets if t.conversation_id == conversation_id]
    return sorted(tickets, key=lambda t: t.created_at)


def approve_ticket(ticket_id: str, resolved_by: str = "user") -> ApprovalTicket | None:
    """Mark a ticket as approved. Returns updated ticket or None if not found."""
    with _state_lock:
        ticket = _all_tickets.get(ticket_id)
        if not ticket or ticket.status != TicketStatus.PENDING:
            return None
        ticket.status = TicketStatus.APPROVED
        ticket.resolved_at = datetime.now(timezone.utc).isoformat()
        ticket.resolved_by = resolved_by
        _pending_index.pop(ticket_id, None)
        _rewrite_tickets()
    _log_approval_md("APPROVED", ticket)
    return ticket


def reject_ticket(ticket_id: str, resolved_by: str = "user") -> ApprovalTicket | None:
    """Mark a ticket as rejected."""
    with _state_lock:
        ticket = _all_tickets.get(ticket_id)
        if not ticket or ticket.status != TicketStatus.PENDING:
            return None
        ticket.status = TicketStatus.REJECTED
        ticket.resolved_at = datetime.now(timezone.utc).isoformat()
        ticket.resolved_by = resolved_by
        _pending_index.pop(ticket_id, None)
        _rewrite_tickets()
    _log_approval_md("REJECTED", ticket)
    return ticket


def mark_applied(ticket_id: str, result: str = "") -> ApprovalTicket | None:
    """Mark an approved ticket as successfully applied."""
    with _state_lock:
        ticket = _all_tickets.get(ticket_id)
        if not ticket or ticket.status != TicketStatus.APPROVED:
            return None
        ticket.status = TicketStatus.APPLIED
        ticket.execution_result = result
        _rewrite_tickets()
    _log_approval_md("APPLIED", ticket)
    return ticket


def mark_failed(ticket_id: str, error: str = "") -> ApprovalTicket | None:
    """Mark an approved ticket as failed during execution."""
    with _state_lock:
        ticket = _all_tickets.get(ticket_id)
        if not ticket:
            return None
        ticket.status = TicketStatus.FAILED
        ticket.execution_result = error
        _rewrite_tickets()
    _log_approval_md("FAILED", ticket)
    return ticket


def get_history(
    conversation_id: str = "", limit: int = 50
) -> list[ApprovalTicket]:
    """Return resolved tickets (most recent first)."""
    tickets = [
        t for t in _all_tickets.values()
        if t.status != TicketStatus.PENDING
    ]
    if conversation_id:
        tickets = [t for t in tickets if t.conversation_id == conversation_id]
    tickets.sort(key=lambda t: t.resolved_at or t.created_at, reverse=True)
    return tickets[:limit]


def reload_from_disk() -> None:
    """Force reload from JSONL (for recovery/testing)."""
    _load_tickets_from_disk()
