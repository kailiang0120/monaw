"""Tests for the execution resume engine – payload hash validation, executor dispatch."""

import importlib
import json
from unittest.mock import MagicMock, patch

import pytest

from app.agent.approval_broker import (
    ApprovalTicket,
    TicketStatus,
    create_ticket,
    approve_ticket,
    _all_tickets,
    _pending_index,
)
from app.agent.execution_resume import (
    register_executor,
    resume_approved_ticket,
    get_registered_executors,
    _EXECUTORS,
)


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    """Redirect broker persistence and reset executor registry."""
    import app.agent.approval_broker as broker_mod
    monkeypatch.setattr(broker_mod, "_TICKETS_FILE", tmp_path / "tickets.jsonl")
    monkeypatch.setattr(broker_mod, "_APPROVAL_LOG", tmp_path / "log.md")
    _all_tickets.clear()
    _pending_index.clear()
    _EXECUTORS.clear()
    yield


class TestRegisterExecutor:
    def test_register_and_list(self):
        register_executor("test_tool", lambda s: '{"ok": true}')
        assert "test_tool" in get_registered_executors()


class TestResumeApprovedTicket:
    def test_executes_on_approved(self):
        register_executor("ctrl_delete", lambda s: '{"status": "ok", "deleted": "f.txt"}')
        t = create_ticket(
            tool_name="ctrl_delete",
            payload={"input_str": '{"path": "f.txt"}', "args": {"path": "f.txt"}},
        )
        approve_ticket(t.id)
        result = resume_approved_ticket(t)
        assert t.status == TicketStatus.APPLIED

    def test_rejects_hash_mismatch(self):
        register_executor("ctrl_move", lambda s: '{"status": "ok"}')
        t = create_ticket(
            tool_name="ctrl_move",
            payload={"input_str": '{"source": "a"}'},
        )
        approve_ticket(t.id)
        t.payload["input_str"] = '{"source": "b"}'
        result = resume_approved_ticket(t)
        assert t.status == TicketStatus.FAILED
        assert "hash mismatch" in t.execution_result.lower()

    def test_fails_on_missing_executor(self):
        t = create_ticket(tool_name="no_such_tool", payload={"input_str": "{}"})
        approve_ticket(t.id)
        result = resume_approved_ticket(t)
        assert t.status == TicketStatus.FAILED
        assert "no executor" in t.execution_result.lower()

    def test_fails_on_executor_error(self):
        def bad_executor(s: str) -> str:
            raise RuntimeError("disk full")

        register_executor("ctrl_copy", bad_executor)
        t = create_ticket(tool_name="ctrl_copy", payload={"input_str": '{"source": "a"}'})
        approve_ticket(t.id)
        resume_approved_ticket(t)
        assert t.status == TicketStatus.FAILED
        assert "disk full" in t.execution_result

    def test_fails_on_executor_returning_error_status(self):
        register_executor("ctrl_rename", lambda s: '{"status": "error", "error": "not found"}')
        t = create_ticket(tool_name="ctrl_rename", payload={"input_str": '{"path": "x"}'})
        approve_ticket(t.id)
        resume_approved_ticket(t)
        assert t.status == TicketStatus.FAILED

    def test_skips_non_approved(self):
        t = create_ticket(tool_name="ctrl_delete")
        result = resume_approved_ticket(t)
        assert t.status == TicketStatus.PENDING

    def test_computer_functions_kill_process_resume_executor(self, monkeypatch):
        desktop_tools = importlib.import_module("app.skills.computer_use.tools")

        calls = []

        def fake_terminate(name_or_pid: str, force: bool = False) -> str:
            calls.append((name_or_pid, force))
            return json.dumps({
                "status": "ok",
                "killed": [{"pid": int(name_or_pid), "name": "notepad.exe"}],
            })

        monkeypatch.setattr(desktop_tools, "_terminate_process", fake_terminate)
        register_executor("computer_functions_kill_process", desktop_tools._resume_computer_functions_kill_process)
        assert "computer_functions_kill_process" in get_registered_executors()

        t = create_ticket(tool_name="computer_functions_kill_process", payload={"name_or_pid": "1234", "force": True})
        approve_ticket(t.id)

        result = resume_approved_ticket(t)

        assert result.status == TicketStatus.APPLIED
        assert calls == [("1234", True)]
        assert "notepad.exe" in result.execution_result
