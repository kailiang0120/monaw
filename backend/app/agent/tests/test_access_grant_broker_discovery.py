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
    get_resume_decision,
    register_pending_resume,
    signal_resume,
)
from app.agent.run_context import reset_current_conversation_id, set_current_conversation_id


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


def test_register_pending_resume_handles_decision_signaled_before_waiter():
    async def scenario():
        signal_resume("grant-race", "session")
        event = register_pending_resume("grant-race")
        assert event.is_set()
        assert get_resume_decision("grant-race") == "session"
        cleanup_resume("grant-race")

    asyncio.run(scenario())
