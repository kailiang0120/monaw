"""P7 process-death acceptance through the real durable tool pipeline."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.agent.database import Database
from app.agent.durable.journal import Journal
from app.agent.durable.recovery import OwnerProcessIdentity, owner_process_proof, recover_startup_runs
from app.agent.file_mutation.snapshot_store import SnapshotStore
from app.agent.file_mutation.verifier import FileMutationVerifier


_CHILD_DRIVER = r'''
import asyncio
import json
import os
import sys
from pathlib import Path

from app.agent.database import Database
from app.agent.durable.turn_run import create_durable_run
from app.agent.llm_client import LLMResponse, ToolCallRequest
from app.agent.run_context import (
    set_current_control_session_id,
    set_current_execution_source,
    set_current_principal_id,
)
from app.agent.conversation_workspace import set_workspace_dir
from app.agent.settings_store import AgentSettings, PathRule, save_agent_settings
from app.agent.tool_registry import ToolRegistry
from app.agent.turn_loop import TurnLoop
from app.skills.filesystem import file_ops

fault, conversation_id, workspace, target_path, call_counter = sys.argv[1:]
workspace = Path(workspace)
target = Path(target_path)
counter = Path(call_counter)
runtime = Path(os.environ["AGENT_RUNTIME_DIR"])

db = Database()
db.init_db()
principal_id = "p7-principal"
session_id = "p7-session"
settings = AgentSettings()
settings.permissions.mode = "full_access"
settings.permissions.blocked_roots = []
settings.permissions.path_rules = [PathRule(
    path=str(workspace), read=True, write=True, delete=True, launch=False,
)]
save_agent_settings(settings, settings_path=runtime / "settings.json")

durable_run = create_durable_run(
    conversation_id=conversation_id,
    execution_source="desktop",
    principal_id=principal_id,
    control_session_id=session_id,
    workspace_dir=str(workspace),
    run_id="p7-run-" + fault,
    turn_id="p7-turn-" + fault,
    db=db,
)


class FakeProvider:
    """Deterministic provider: one native write request, then one final answer."""
    provider = "fake"
    model_name = "p7-fake"
    calls = 0

    async def chat_with_tools(self, messages, tools, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    call_id="p7-write",
                    tool_name="fs_write",
                    arguments={"path": str(target), "content": "after image P7\n", "overwrite": True},
                )],
                finish_reason="tool_calls",
            )
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(
                call_id="p7-final",
                tool_name="final_answer",
                arguments={"answer": "Finished."},
            )],
            finish_reason="tool_calls",
        )


class FakeMemory:
    """Small durable memory adapter using the production SQLite commit methods."""
    def get_or_create(self, _conversation_id, title=""):
        return {"title": title}

    def set_task_goal(self, conversation_id, goal):
        db.update_conversation(conversation_id, task_goal=goal)

    def set_active_task(self, _conversation_id, _task):
        return None

    async def ensure_context_fits(self, *_args, **_kwargs):
        return None

    def build_llm_messages(self, _conversation_id):
        return []

    def add_tool_outcome(self, conversation_id, tool_name, result):
        db.add_tool_outcome(conversation_id, tool_name, result)
        if fault == "after_result_submit":
            os._exit(75)

    async def persist_turn(
        self, conversation_id, user_message, assistant_message, *, tool_calls,
        turn_id, status="complete", **_kwargs,
    ):
        messages = [
            {"role": "user", "content": user_message},
            {
                "role": "assistant", "content": assistant_message,
                "status": status, "tool_calls": tool_calls,
            },
        ]
        db.persist_turn_messages(turn_id, conversation_id, messages)
        return []

    def clear_task_progress(self, conversation_id):
        db.update_conversation(conversation_id, task_goal="")


class FakeObservability:
    def start_run(self, **_kwargs):
        return "p7-fake-observation"

    def log_event(self, **_kwargs):
        return "p7-fake-event"

    def log_error(self, **_kwargs):
        return "p7-fake-error"

    def finish_run(self, **_kwargs):
        return None

    def finish_open_run_for_conversation(self, **_kwargs):
        return None


if fault in {"before_intent", "after_intent"}:
    import app.agent.harness.tool_runner as tool_runner
    prepare = tool_runner.prepare_tool_operation
    if fault == "before_intent":
        def injected_prepare(*_args, **_kwargs):
            os._exit(71)
    else:
        def injected_prepare(*args, **kwargs):
            operation = prepare(*args, **kwargs)
            os._exit(72)
    tool_runner.prepare_tool_operation = injected_prepare


def invoke_fs_write(**arguments):
    counter.write_text("1\n", encoding="ascii")
    if fault == "write_success_before_result_commit":
        def exit_before_native_verification(*_args, **_kwargs):
            os._exit(76)

        # The real native write has completed before file_ops reaches this
        # verifier entry. Exit here leaves the durable operation executing.
        file_ops._p3_verify_effect = exit_before_native_verification
    if fault == "effect_in_progress":
        original_fdopen = os.fdopen

        class CrashAfterPartialWrite:
            def __init__(self, wrapped):
                self.wrapped = wrapped

            def __enter__(self):
                self.wrapped.__enter__()
                return self

            def __exit__(self, *exc_info):
                return self.wrapped.__exit__(*exc_info)

            def seek(self, *args, **kwargs):
                return self.wrapped.seek(*args, **kwargs)

            def truncate(self, *args, **kwargs):
                return self.wrapped.truncate(*args, **kwargs)

            def write(self, data):
                self.wrapped.write(data[:max(1, len(data) // 2)])
                self.wrapped.flush()
                os._exit(73)

            def flush(self):
                return self.wrapped.flush()

        def injected_fdopen(fd, mode="r", *args, **kwargs):
            wrapped = original_fdopen(fd, mode, *args, **kwargs)
            return CrashAfterPartialWrite(wrapped) if mode == "r+b" else wrapped

        os.fdopen = injected_fdopen

    output = file_ops.file_write(**arguments)
    if fault == "after_write_before_result":
        os._exit(74)
    return output


registry = ToolRegistry([{
    "name": "fs_write",
    "description": "Write one file for P7 acceptance.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "overwrite": {"type": "boolean"},
        },
        "required": ["path", "content", "overwrite"],
    },
    "callable": invoke_fs_write,
    "domain": "filesystem",
    "execution_mode": "sync_stateless",
    "metadata": {
        "durable_builtin": True,
        "mutates_state": True,
        "risk_level": "low",
        "resource_locks": ["p7-target"],
    },
}])
loop = TurnLoop(
    llm_client=FakeProvider(),
    registry=registry,
    memory=FakeMemory(),
    observability=FakeObservability(),
    max_turn_seconds=20,
)
workspace_token = set_workspace_dir(str(workspace))
session_token = set_current_control_session_id(session_id)
source_token = set_current_execution_source("desktop")
principal_token = set_current_principal_id(principal_id)


async def run_turn():
    return [event async for event in loop.run(
        "Write the requested file.",
        conversation_id,
        system_prompt="",
        durable_run=durable_run,
    )]


try:
    asyncio.run(run_turn())
finally:
    loop.shutdown()
    db.close()
    reset = __import__("app.agent.conversation_workspace", fromlist=["reset_workspace_dir"])
    reset.reset_workspace_dir(workspace_token)
    from app.agent.run_context import (
        reset_current_control_session_id,
        reset_current_execution_source,
        reset_current_principal_id,
    )
    reset_current_control_session_id(session_token)
    reset_current_execution_source(source_token)
    reset_current_principal_id(principal_token)
'''


@pytest.mark.parametrize(
    ("fault", "exit_code", "before_bytes", "after_bytes", "operation_status", "tool_outcomes"),
    [
        ("before_intent", 71, b"before image\n", b"before image\n", None, 0),
        ("after_intent", 72, b"before image\n", b"before image\n", "cancelled", 0),
        ("effect_in_progress", 73, b"before image\n", b"after i", "uncertain", 0),
        ("write_success_before_result_commit", 76, b"before image\n", b"after image P7\n", "verified", 0),
        ("after_write_before_result", 74, b"before image\n", b"after image P7\n", "verified", 0),
        ("after_result_submit", 75, b"before image\n", b"after image P7\n", "verified", 1),
    ],
)
def test_p7_real_process_crash_matrix_recovers_without_replaying_native_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    exit_code: int,
    before_bytes: bytes,
    after_bytes: bytes,
    operation_status: str | None,
    tool_outcomes: int,
):
    """Kill a fake-provider TurnLoop at each durable/file boundary, then restart recovery."""
    home = tmp_path / "isolated-monaw-home"
    runtime = home / "runtime"
    workspace = tmp_path / "workspace"
    runtime.mkdir(parents=True)
    workspace.mkdir()
    target = workspace / "p7-target.txt"
    target.write_bytes(before_bytes)
    call_counter = runtime / "fake-provider-write-count.txt"
    conversation_id = "p7-conversation-" + fault

    seed_db = Database(runtime / "agent.db")
    seed_db.init_db()
    seed_db.create_conversation(conversation_id, "P7 crash acceptance", str(workspace))
    seed_db.close()

    backend_dir = Path(__file__).resolve().parents[3]
    environment = os.environ.copy()
    environment["MONAW_HOME"] = str(home)
    environment["AGENT_RUNTIME_DIR"] = str(runtime)
    environment["MONAW_DISABLE_BUILTIN_MCP"] = "1"
    environment["MONAW_CONTROL_SECRET"] = "p7-isolated-control-secret-at-least-32-chars"
    environment["PYTHONPATH"] = str(backend_dir) + os.pathsep + environment.get("PYTHONPATH", "")
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            _CHILD_DRIVER,
            fault,
            conversation_id,
            str(workspace),
            str(target),
            str(call_counter),
        ],
        cwd=backend_dir,
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert child.returncode == exit_code, child.stderr
    assert target.read_bytes() == after_bytes
    if exit_code >= 73:
        assert call_counter.read_text(encoding="ascii").strip() == "1"
    else:
        assert not call_counter.exists()

    db = Database(runtime / "agent.db")
    journal = Journal(db)
    run_id = "p7-run-" + fault
    operations = journal.list_operations(run_id)
    if operation_status is None:
        assert operations == []
    else:
        assert len(operations) == 1
    if fault == "write_success_before_result_commit":
        assert operations[0]["status"] == "executing"
    assert db.fetchone(
        "SELECT COUNT(*) FROM tool_outcomes WHERE conversation_id = ?", (conversation_id,)
    )[0] == tool_outcomes
    # The fake provider's tool response has not reached the atomic turn checkpoint.
    assert db.fetchone(
        "SELECT COUNT(*) FROM turn_checkpoints WHERE turn_id = ?", ("p7-turn-" + fault,)
    )[0] == 0

    import app.agent.file_mutation.snapshot_store as snapshot_store_module

    monkeypatch.setattr(snapshot_store_module, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(snapshot_store_module, "_DEFAULT_STORE", None)
    store = SnapshotStore(runtime / "file_mutation" / "snapshots")
    recovery_owner = OwnerProcessIdentity(runtime / "owner_locks")
    recovery_owner_id = recovery_owner.acquire()
    try:
        report = recover_startup_runs(
            journal=journal,
            recovery_owner_instance_id=recovery_owner_id,
            owner_proof=lambda owner: owner_process_proof(
                owner, lock_dir=runtime / "owner_locks"
            ),
            read_only_verifier=FileMutationVerifier(
                store,
                read_policy=lambda path: Path(path).resolve().is_relative_to(workspace.resolve()),
            ).verify,
        )
        assert report["recovered"] == 1
        assert report["errors"] == 0
        if fault == "write_success_before_result_commit":
            assert report["verified_operations"] == 1
        assert target.read_bytes() == after_bytes
        assert db.fetchone(
            "SELECT COUNT(*) FROM tool_outcomes WHERE conversation_id = ?", (conversation_id,)
        )[0] == tool_outcomes
        if operation_status is not None:
            recovered_operation = journal.list_operations(run_id)[0]
            assert recovered_operation["status"] == operation_status
        if fault == "effect_in_progress":
            recovered_run = journal.load_run(run_id)
            assert recovered_run["status"] == "needs_review"
        if fault in {"after_write_before_result", "after_result_submit"}:
            assert journal.list_operations(run_id)[0]["effect_class"] == "verifiable_write"
    finally:
        recovery_owner.close()

    # A second fresh owner repeats startup inspection. It must never redispatch
    # the provider or reapply the file write after the first handoff.
    second_recovery_owner = OwnerProcessIdentity(runtime / "owner_locks")
    second_owner_id = second_recovery_owner.acquire()
    try:
        second_report = recover_startup_runs(
            journal=journal,
            recovery_owner_instance_id=second_owner_id,
            owner_proof=lambda owner: owner_process_proof(
                owner, lock_dir=runtime / "owner_locks"
            ),
            read_only_verifier=FileMutationVerifier(
                store,
                read_policy=lambda path: Path(path).resolve().is_relative_to(workspace.resolve()),
            ).verify,
        )
        assert second_report["recovered"] == 1
        assert second_report["errors"] == 0
        assert target.read_bytes() == after_bytes
        if operation_status is not None:
            assert journal.list_operations(run_id)[0]["status"] == operation_status
        if exit_code >= 73:
            assert call_counter.read_text(encoding="ascii").strip() == "1"
        else:
            assert not call_counter.exists()
    finally:
        second_recovery_owner.close()
        db.close()
