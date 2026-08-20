"""Access grant broker — interactive permission flow for unknown apps/paths.

When the agent encounters an app or path that is neither blacklisted nor
whitelisted, an AccessGrantTicket is created and an SSE event is emitted
to the frontend.  The user chooses one of:
  - "once"    → allow this one action, then forget
  - "session" → allow for the rest of this session (in-memory + SQLite)
  - "always"  → persist to settings.json allowlist permanently
  - "deny"    → block the action

Tickets are stored in-memory (not JSONL) since they are transient.

Resume signaling:
  The react loop calls register_pending_resume(ticket_id) to register an
  asyncio.Event it will wait on.  The resolve endpoint calls signal_resume
  after updating the ticket so the loop wakes up with the real decision.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field

from app.agent.database import get_db
from app.agent.ui_events import publish_ui_event
from app.agent.run_context import (
    current_control_session_id,
    current_conversation_id,
    current_execution_source,
    current_interactive,
    current_permission_profile_id,
    current_principal_id,
)
from app.agent.controller_policy import (
    AppEntry,
    add_allowlisted_app,
    update_permitted_roots,
    load_policy,
)
from app.agent.settings_store import (
    PathRule,
    load_agent_settings,
    save_agent_settings,
)


class AccessGrantTicket(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    conversation_id: str = ""
    control_session_id: str = ""
    execution_source: str = "desktop"
    principal_id: str = ""
    permission_profile_id: str = ""
    interactive: bool = True
    target_type: str = ""  # "app" | "path"
    target_identifier: str = ""
    display_name: str = ""
    action_context: str = ""
    requested_access: Literal["read", "write", "delete", "launch"] = "read"
    status: str = "pending"  # pending | granted | denied
    decision: str = ""  # once | session | always | deny
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    expires_at: str = Field(
        default_factory=lambda: (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    )
    payload_hash: str = ""
    superseded_by: str = ""
    resolved_at: str = ""

    def compute_hash(self) -> str:
        payload = {
            "conversation_id": self.conversation_id,
            "control_session_id": self.control_session_id,
            "execution_source": self.execution_source,
            "principal_id": self.principal_id,
            "permission_profile_id": self.permission_profile_id,
            "target_type": self.target_type,
            "target_identifier": self.target_identifier,
            "action_context": self.action_context,
            "requested_access": self.requested_access,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── In-memory ticket store ────────────────────────────────────────────────────

# C1: Single lock guards all mutable module-level state.
_state_lock: threading.RLock = threading.RLock()

_pending: dict[str, AccessGrantTicket] = {}
_all: dict[str, AccessGrantTicket] = {}

# ── Resume signaling (set by react loop; signaled by resolve endpoint) ────────

_resume_events: dict[str, asyncio.Event] = {}
_resume_decisions: dict[str, str] = {}


def create_grant_ticket(
    *,
    conversation_id: str = "",
    control_session_id: str = "",
    execution_source: str = "",
    principal_id: str = "",
    permission_profile_id: str = "",
    interactive: bool | None = None,
    target_type: str = "app",
    target_identifier: str = "",
    display_name: str = "",
    action_context: str = "",
    requested_access: str = "read",
) -> AccessGrantTicket:
    """Create a pending access grant ticket."""
    ticket = AccessGrantTicket(
        conversation_id=conversation_id or current_conversation_id(),
        control_session_id=control_session_id or current_control_session_id(),
        execution_source=execution_source or current_execution_source(),
        principal_id=principal_id or current_principal_id(),
        permission_profile_id=permission_profile_id or current_permission_profile_id(),
        interactive=current_interactive() if interactive is None else bool(interactive),
        target_type=target_type,
        target_identifier=target_identifier,
        display_name=display_name or target_identifier,
        action_context=action_context,
        requested_access=(requested_access if requested_access in {"read", "write", "delete", "launch"} else "read"),
    )
    ticket.payload_hash = ticket.compute_hash()
    superseded_ids: list[str] = []
    with _state_lock:
        for existing in list(_pending.values()):
            if (
                ticket.target_identifier
                and existing.conversation_id == ticket.conversation_id
                and existing.control_session_id == ticket.control_session_id
                and existing.execution_source == ticket.execution_source
                and existing.principal_id == ticket.principal_id
                and existing.target_type == ticket.target_type
                and existing.target_identifier == ticket.target_identifier
            ):
                existing.status = "superseded"
                existing.superseded_by = ticket.id
                existing.resolved_at = datetime.now(timezone.utc).isoformat()
                _pending.pop(existing.id, None)
                superseded_ids.append(existing.id)
        _pending[ticket.id] = ticket
        _all[ticket.id] = ticket
    publish_ui_event(
        "access_grant.created",
        {
            "ticket_id": ticket.id,
            "conversation_id": ticket.conversation_id,
            "target_type": ticket.target_type,
            "target_identifier": ticket.target_identifier,
            "display_name": ticket.display_name,
            "action_context": ticket.action_context,
            "requested_access": ticket.requested_access,
        },
    )
    for superseded_id in superseded_ids:
        signal_resume(superseded_id, "superseded")
        publish_ui_event(
            "access_grant.changed",
            {"ticket_id": superseded_id, "conversation_id": ticket.conversation_id, "status": "superseded"},
        )
    return ticket


def get_pending_grants(conversation_id: str = "") -> list[AccessGrantTicket]:
    tickets = list(_pending.values())
    if conversation_id:
        tickets = [t for t in tickets if t.conversation_id == conversation_id]
    return sorted(tickets, key=lambda t: t.created_at)


def get_grant_ticket(ticket_id: str) -> AccessGrantTicket | None:
    return _all.get(ticket_id)


def grant_validation_error(
    ticket: AccessGrantTicket,
    *,
    expected_session_id: str = "",
    expected_conversation_id: str = "",
    expected_execution_source: str = "",
    require_granted: bool = False,
) -> str:
    expected_status = "granted" if require_granted else "pending"
    if ticket.status != expected_status:
        return f"ticket is {ticket.status}"
    try:
        if datetime.fromisoformat(ticket.expires_at) <= datetime.now(timezone.utc):
            return "ticket is expired"
    except ValueError:
        return "ticket expiry is invalid"
    if expected_session_id and ticket.control_session_id != expected_session_id:
        return "ticket belongs to another control session"
    if expected_conversation_id and ticket.conversation_id != expected_conversation_id:
        return "ticket belongs to another conversation"
    if expected_execution_source and ticket.execution_source != expected_execution_source:
        return "ticket belongs to another execution source"
    if ticket.compute_hash() != ticket.payload_hash:
        return "ticket payload hash mismatch"
    return ""


def _session_grant_identifier(identifier: str) -> str:
    return "|".join((
        current_control_session_id(),
        current_execution_source(),
        current_principal_id(),
        current_permission_profile_id(),
        current_conversation_id(),
        identifier,
    ))


def resolve_grant(
    ticket_id: str,
    decision: str,
    *,
    expected_session_id: str = "",
) -> AccessGrantTicket | None:
    """Resolve an access grant ticket with the user's decision.

    decision: "once" | "session" | "always" | "deny"
    """
    with _state_lock:
        ticket = _pending.get(ticket_id)
        if ticket is None or grant_validation_error(ticket, expected_session_id=expected_session_id):
            return None
        _pending.pop(ticket_id, None)

    ticket.resolved_at = datetime.now(timezone.utc).isoformat()
    ticket.decision = decision

    if not ticket.interactive and decision in {"session", "always"}:
        ticket.status = "denied"
        return ticket

    if decision == "deny":
        ticket.status = "denied"
        return ticket

    ticket.status = "granted"

    if decision == "always":
        _persist_permanent_grant(ticket)
    elif decision == "session":
        get_db().add_session_grant(
            ticket.target_type, _session_grant_identifier(ticket.target_identifier), "session"
        )
    elif decision == "once":
        get_db().add_session_grant(
            ticket.target_type, _session_grant_identifier(ticket.target_identifier), "once"
        )

    return ticket


def check_session_grant(target_type: str, identifier: str) -> bool:
    """Check if a session grant exists for this target."""
    scoped_identifier = _session_grant_identifier(identifier)
    grant = get_db().check_session_grant(target_type, scoped_identifier)
    if grant == "once":
        get_db().consume_once_grant(target_type, scoped_identifier)
        return True
    return grant is not None


def clear_session_grants() -> None:
    """Clear all session grants (called on app restart)."""
    get_db().clear_session_grants()


def clear_all_grants() -> dict[str, int]:
    """Clear transient access-grant tickets, resume state, and DB session grants."""
    with _state_lock:
        counts = {
            "access_grant_tickets": len(_all),
            "pending_access_grants": len(_pending),
            "access_grant_resume_events": len(_resume_events),
        }
        _pending.clear()
        _all.clear()
        _resume_events.clear()
        _resume_decisions.clear()
    get_db().clear_session_grants()
    publish_ui_event("access_grant.deleted", {"all": True})
    return counts


def enforce_retention(max_age_days: int = 30, max_items: int = 1000) -> dict[str, int]:
    """Bound resolved in-memory grant history by age and count."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(max_age_days)))

    def _ticket_time(ticket: AccessGrantTicket) -> datetime:
        for value in (ticket.resolved_at, ticket.created_at):
            try:
                parsed = datetime.fromisoformat(value)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return datetime.now(timezone.utc)

    with _state_lock:
        original_count = len(_all)
        retained = {
            ticket_id: ticket
            for ticket_id, ticket in _all.items()
            if ticket.status == "pending" or _ticket_time(ticket) >= cutoff
        }
        resolved = [
            ticket
            for ticket in retained.values()
            if ticket.status != "pending"
        ]
        overflow = max(0, len(resolved) - max(0, int(max_items)))
        if overflow:
            drop_ids = {
                ticket.id
                for ticket in sorted(resolved, key=_ticket_time)[:overflow]
            }
            retained = {
                ticket_id: ticket
                for ticket_id, ticket in retained.items()
                if ticket_id not in drop_ids
            }
        _all.clear()
        _all.update(retained)
        _pending.clear()
        _pending.update({
            ticket_id: ticket
            for ticket_id, ticket in _all.items()
            if ticket.status == "pending"
        })
    return {"access_grants_removed": max(0, original_count - len(_all))}


def register_pending_resume(ticket_id: str) -> asyncio.Event:
    """Register an asyncio.Event the react loop will wait on. Call from async context."""
    event = asyncio.Event()
    with _state_lock:
        _resume_events[ticket_id] = event
        if ticket_id in _resume_decisions:
            event.set()
    return event


def signal_resume(ticket_id: str, decision: str) -> None:
    """Signal the waiting react loop that the ticket was resolved. Call from async context."""
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


def _discover_exe_for_alias(alias: str) -> str | None:
    """Attempt to locate the executable for a human-friendly app alias.

    Search order:
      1. Direct absolute path check
      2. shutil.which (alias and alias.exe)
      3. Space-collapsed / hyphenated variants via shutil.which
      4. Windows Registry App Paths (HKLM + HKCU)
      5. Start Menu .lnk shortcut resolution
    """
    import glob
    import os
    import shutil as _shutil

    alias_norm = alias.strip()
    if not alias_norm:
        return None

    def _candidate_exe_names(name: str) -> list[str]:
        lower = name.lower()
        no_space = lower.replace(" ", "")
        dash = lower.replace(" ", "-")
        title = " ".join(part.capitalize() for part in lower.split())
        return list(
            dict.fromkeys(
                [
                    f"{lower}.exe",
                    f"{no_space}.exe",
                    f"{dash}.exe",
                    f"{title}.exe",
                ]
            )
        )

    def _candidate_install_globs(name: str) -> list[str]:
        no_space = name.replace(" ", "")
        title = " ".join(part.capitalize() for part in name.split())
        return [
            os.path.expandvars(rf"%ProgramFiles%\{title}\**\*.exe"),
            os.path.expandvars(rf"%ProgramFiles%\{title}\*.exe"),
            os.path.expandvars(rf"%ProgramFiles%\{title.replace(' ', '')}\**\*.exe"),
            os.path.expandvars(rf"%ProgramFiles%\{title.replace(' ', '')}\*.exe"),
            os.path.expandvars(rf"%ProgramFiles%\{title.split(' ')[0]}\**\{title}.exe"),
            os.path.expandvars(rf"%ProgramFiles(x86)%\{title}\**\*.exe"),
            os.path.expandvars(rf"%ProgramFiles(x86)%\{title}\*.exe"),
            os.path.expandvars(rf"%LocalAppData%\Programs\{title}\**\*.exe"),
            os.path.expandvars(rf"%LocalAppData%\Programs\{title}\*.exe"),
            os.path.expandvars(rf"%ProgramFiles%\Docker\Docker\Docker Desktop.exe"),
            os.path.expandvars(rf"%ProgramFiles%\Docker\Docker\resources\Docker Desktop.exe"),
        ]

    # 1. Already an absolute file path
    if os.path.isabs(alias_norm) and os.path.isfile(alias_norm):
        return alias_norm

    # 2. Direct PATH lookup
    found = _shutil.which(alias_norm) or _shutil.which(alias_norm + ".exe")
    if found:
        return found

    # 3. Try common alias normalizations (spaces → removed / hyphens)
    variants = {
        alias_norm.replace(" ", ""),
        alias_norm.replace(" ", "-"),
        alias_norm.replace(" ", "_"),
    }
    for variant in variants:
        found = _shutil.which(variant) or _shutil.which(variant + ".exe")
        if found:
            return found

    # 4. Windows Registry App Paths — authoritative for installed GUI apps
    try:
        import winreg

        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for suffix in _candidate_exe_names(alias_norm):
                try:
                    key_path = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{suffix}"
                    with winreg.OpenKey(hive, key_path) as key:
                        exe_path, _ = winreg.QueryValueEx(key, "")
                        if exe_path and os.path.isfile(exe_path):
                            return exe_path
                except (OSError, FileNotFoundError):
                    continue

            # Installed apps may only be discoverable in Uninstall keys.
            uninstall_roots = (
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
            )
            for root in uninstall_roots:
                try:
                    with winreg.OpenKey(hive, root) as uninstall_key:
                        index = 0
                        while True:
                            try:
                                subkey_name = winreg.EnumKey(uninstall_key, index)
                            except OSError:
                                break
                            index += 1
                            try:
                                with winreg.OpenKey(uninstall_key, subkey_name) as app_key:
                                    display_name, _ = winreg.QueryValueEx(app_key, "DisplayName")
                                    if alias_norm.lower() not in str(display_name).lower():
                                        continue
                                    display_icon, _ = winreg.QueryValueEx(app_key, "DisplayIcon")
                                    if display_icon:
                                        icon_path = str(display_icon).split(",")[0].strip('" ')
                                        if os.path.isfile(icon_path):
                                            # Prefer launchable app binaries over installer stubs.
                                            if "installer" in os.path.basename(icon_path).lower():
                                                continue
                                            return icon_path
                                    install_location, _ = winreg.QueryValueEx(app_key, "InstallLocation")
                                    if install_location and os.path.isdir(install_location):
                                        for exe_name in _candidate_exe_names(alias_norm):
                                            candidate = os.path.join(install_location, exe_name)
                                            if os.path.isfile(candidate):
                                                return candidate
                            except (OSError, FileNotFoundError):
                                continue
                except (OSError, FileNotFoundError):
                    continue
    except ImportError:
        pass  # Non-Windows — skip registry

    # 5. Common install roots by heuristic
    for pattern in _candidate_install_globs(alias_norm):
        if "*" not in pattern:
            if os.path.isfile(pattern):
                return pattern
            continue
        for candidate in glob.glob(pattern, recursive=True):
            if os.path.isfile(candidate):
                return candidate

    # 6. Start Menu .lnk shortcut scan
    try:
        import win32com.client

        shell = win32com.client.Dispatch("WScript.Shell")
        search_dirs = [
            os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),
            os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
        ]
        alias_lower = alias_norm.lower()
        for search_dir in search_dirs:
            for lnk_path in glob.glob(os.path.join(search_dir, "**", "*.lnk"), recursive=True):
                lnk_name = os.path.splitext(os.path.basename(lnk_path))[0].lower()
                # Match if alias is contained in the shortcut name (case-insensitive)
                if alias_lower in lnk_name or lnk_name in alias_lower:
                    try:
                        shortcut = shell.CreateShortcut(lnk_path)
                        target = shortcut.Targetpath
                        if target and os.path.isfile(target):
                            return target
                    except Exception:
                        continue
    except (ImportError, Exception):
        pass  # win32com unavailable or scan failed

    return None


def _persist_permanent_grant(ticket: AccessGrantTicket) -> None:
    """Add the target to settings.json permanently."""
    if ticket.target_type == "app":
        discovered = _discover_exe_for_alias(ticket.target_identifier)
        exe_paths = [discovered] if discovered else []
        entry = AppEntry(
            alias=ticket.target_identifier,
            display_name=ticket.display_name,
            exe_paths=exe_paths,
        )
        add_allowlisted_app(entry)
    elif ticket.target_type == "path":
        state = load_policy()
        roots = list(state.permitted_roots)
        if ticket.target_identifier not in roots:
            roots.append(ticket.target_identifier)
        update_permitted_roots(
            roots,
            new_rule=_path_rule_for_grant(ticket),
            merge_existing_rule=True,
        )


def _path_rule_for_grant(ticket: AccessGrantTicket) -> PathRule:
    """Translate the requested path action into the narrowest permanent rule."""
    requested_access = ticket.requested_access
    is_delete = requested_access == "delete"
    is_write = requested_access in {"write", "delete"}
    is_launch = requested_access == "launch"
    read = requested_access in {"read", "write", "delete"}
    return PathRule(
        path=ticket.target_identifier,
        read=read,
        write=is_write,
        delete=is_delete,
        launch=is_launch,
        enabled=True,
    )
