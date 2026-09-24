from __future__ import annotations

import asyncio
import os
import shutil
import glob

import pytest

from app.agent.access_grant_broker import (
    _all,
    _discover_exe_for_alias,
    _pending,
    _resume_decisions,
    _resume_events,
    cleanup_resume,
    create_grant_ticket,
    _path_rule_for_grant,
    get_resume_decision,
    register_pending_resume,
    resolve_grant,
    signal_resume,
)
from app.agent.run_context import (
    reset_current_conversation_id,
    reset_current_interactive,
    set_current_conversation_id,
    set_current_interactive,
)


@pytest.fixture(autouse=True)
def _clean_access_grants():
    _pending.clear()
    _all.clear()
    _resume_events.clear()
    _resume_decisions.clear()
    yield
    _pending.clear()
    _all.clear()
    _resume_events.clear()
    _resume_decisions.clear()


def test_discover_exe_for_alias_finds_docker_desktop_in_known_location(monkeypatch):
    docker_exe = r"C:\Program Files\Docker\Docker\Docker Desktop.exe"

    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    monkeypatch.setenv("ProgramFiles(x86)", r"C:\Program Files (x86)")
    monkeypatch.setenv("LocalAppData", r"C:\Users\Test\AppData\Local")
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(glob, "glob", lambda *_args, **_kwargs: [])

    original_isfile = os.path.isfile

    def fake_isfile(path: str) -> bool:
        if path == docker_exe:
            return True
        return original_isfile(path)

    monkeypatch.setattr(os.path, "isfile", fake_isfile)

    assert _discover_exe_for_alias("docker desktop") == docker_exe


def test_create_grant_ticket_inherits_current_conversation_context():
    token = set_current_conversation_id("conv-grant")
    try:
        ticket = create_grant_ticket(target_type="app", target_identifier="notepad")
    finally:
        reset_current_conversation_id(token)

    assert ticket.conversation_id == "conv-grant"


def test_permanent_path_rule_uses_structured_access_not_action_prose():
    patch_ticket = create_grant_ticket(
        target_type="path",
        target_identifier="C:/workspace/patch-write.txt",
        action_context="Patch file: C:/workspace/patch-write.txt",
        requested_access="write",
    )
    delete_ticket = create_grant_ticket(
        target_type="path",
        target_identifier="C:/workspace/write-target.txt",
        action_context="Read a file whose name contains write",
        requested_access="delete",
    )

    patch_rule = _path_rule_for_grant(patch_ticket)
    delete_rule = _path_rule_for_grant(delete_ticket)

    assert patch_ticket.requested_access == "write"
    assert patch_rule.write is True
    assert patch_rule.delete is False
    assert delete_rule.delete is True


def test_non_interactive_grant_ticket_cannot_create_persistent_grant():
    token = set_current_interactive(False)
    try:
        ticket = create_grant_ticket(target_type="app", target_identifier="notepad")
    finally:
        reset_current_interactive(token)

    resolved = resolve_grant(ticket.id, "always")

    assert resolved is not None
    assert resolved.status == "denied"
    assert resolved.decision == "always"


def test_register_pending_resume_handles_decision_signaled_before_waiter():
    async def scenario():
        signal_resume("grant-race", "session")
        event = register_pending_resume("grant-race")
        assert event.is_set()
        assert get_resume_decision("grant-race") == "session"
        cleanup_resume("grant-race")

    asyncio.run(scenario())


def _run_in_scope(conversation_id: str, fn):
    from app.agent.run_context import (
        reset_current_control_session_id,
        set_current_control_session_id,
    )

    session_token = set_current_control_session_id("session-a")
    conversation_token = set_current_conversation_id(conversation_id)
    try:
        return fn()
    finally:
        reset_current_conversation_id(conversation_token)
        reset_current_control_session_id(session_token)


@pytest.fixture
def grant_db(tmp_path, monkeypatch):
    from app.agent import access_grant_broker
    from app.agent.database import Database

    db = Database(tmp_path / "agent.db")
    db.init_db()
    monkeypatch.setattr(access_grant_broker, "get_db", lambda: db)
    return db


def test_session_grant_is_scoped_to_the_requesting_run_not_the_resolver(grant_db):
    from app.agent.access_grant_broker import check_session_grant

    ticket = _run_in_scope(
        "conv-1",
        lambda: create_grant_ticket(target_type="path", target_identifier=r"C:\data\a.txt", requested_access="read"),
    )
    # The HTTP resolve handler runs with a default (empty) run context.
    resolve_grant(ticket.id, "session", expected_session_id="session-a")

    assert _run_in_scope("conv-1", lambda: check_session_grant("path", r"C:\data\a.txt", "read"))
    assert not _run_in_scope("conv-2", lambda: check_session_grant("path", r"C:\data\a.txt", "read"))
    assert not check_session_grant("path", r"C:\data\a.txt", "read")


def test_read_grant_does_not_authorize_write_or_delete(grant_db):
    from app.agent.access_grant_broker import check_session_grant

    ticket = _run_in_scope(
        "conv-1",
        lambda: create_grant_ticket(target_type="path", target_identifier=r"C:\data\a.txt", requested_access="read"),
    )
    resolve_grant(ticket.id, "session", expected_session_id="session-a")

    assert not _run_in_scope("conv-1", lambda: check_session_grant("path", r"C:\data\a.txt", "write"))
    assert not _run_in_scope("conv-1", lambda: check_session_grant("path", r"C:\data\a.txt", "delete"))


def test_once_grant_can_only_be_consumed_once(grant_db):
    from app.agent.access_grant_broker import check_session_grant

    ticket = _run_in_scope(
        "conv-1",
        lambda: create_grant_ticket(target_type="path", target_identifier=r"C:\data\a.txt", requested_access="write"),
    )
    resolve_grant(ticket.id, "once", expected_session_id="session-a")

    assert _run_in_scope("conv-1", lambda: check_session_grant("path", r"C:\data\a.txt", "read"))
    assert not _run_in_scope("conv-1", lambda: check_session_grant("path", r"C:\data\a.txt", "write"))
