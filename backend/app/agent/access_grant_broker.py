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
import threading
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.agent.database import get_db
from app.agent.run_context import current_conversation_id
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
    target_type: str = ""  # "app" | "path"
    target_identifier: str = ""
    display_name: str = ""
    action_context: str = ""
    status: str = "pending"  # pending | granted | denied
    decision: str = ""  # once | session | always | deny
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    resolved_at: str = ""


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
    target_type: str = "app",
    target_identifier: str = "",
    display_name: str = "",
    action_context: str = "",
) -> AccessGrantTicket:
    """Create a pending access grant ticket."""
    ticket = AccessGrantTicket(
        conversation_id=conversation_id or current_conversation_id(),
        target_type=target_type,
        target_identifier=target_identifier,
        display_name=display_name or target_identifier,
        action_context=action_context,
    )
    with _state_lock:
        _pending[ticket.id] = ticket
        _all[ticket.id] = ticket
    return ticket


def get_pending_grants(conversation_id: str = "") -> list[AccessGrantTicket]:
    tickets = list(_pending.values())
    if conversation_id:
        tickets = [t for t in tickets if t.conversation_id == conversation_id]
    return sorted(tickets, key=lambda t: t.created_at)


def get_grant_ticket(ticket_id: str) -> AccessGrantTicket | None:
    return _all.get(ticket_id)


def resolve_grant(
    ticket_id: str, decision: str
) -> AccessGrantTicket | None:
    """Resolve an access grant ticket with the user's decision.

    decision: "once" | "session" | "always" | "deny"
    """
    with _state_lock:
        ticket = _pending.pop(ticket_id, None)
    if ticket is None:
        return None

    ticket.resolved_at = datetime.now(timezone.utc).isoformat()
    ticket.decision = decision

    if decision == "deny":
        ticket.status = "denied"
        return ticket

    ticket.status = "granted"

    if decision == "always":
        _persist_permanent_grant(ticket)
    elif decision == "session":
        get_db().add_session_grant(
            ticket.target_type, ticket.target_identifier, "session"
        )
    elif decision == "once":
        get_db().add_session_grant(
            ticket.target_type, ticket.target_identifier, "once"
        )

    return ticket


def check_session_grant(target_type: str, identifier: str) -> bool:
    """Check if a session grant exists for this target."""
    grant = get_db().check_session_grant(target_type, identifier)
    if grant == "once":
        get_db().consume_once_grant(target_type, identifier)
        return True
    return grant is not None


def clear_session_grants() -> None:
    """Clear all session grants (called on app restart)."""
    get_db().clear_session_grants()


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
            update_permitted_roots(roots)
