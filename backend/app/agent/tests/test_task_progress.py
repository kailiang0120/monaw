"""Core task persistence, completion evidence, and useful-progress regressions."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agent.database import Database
from app.agent.harness.progress_monitor import ProgressMonitor
from app.agent.harness.task_progress import TaskProgress
from app.agent.harness.tool_protocol import ToolCallResult
from app.agent.llm_client import LLMResponse, ToolCallRequest
from app.agent.tests.test_file_mutation_integration import (
    native_context as native_file_context,
    _start_operation,
    _invoke_file_write,
)
from app.agent.tests.test_turn_loop import FakeObservability, PersistingFakeMemory
from app.agent.tool_registry import ToolRegistry
from app.agent.turn_loop import TurnLoop


@pytest.fixture
def native_context(native_file_context, monkeypatch):
    """Share the native verifier's isolated snapshot store with task recording."""
    monkeypatch.setattr(
        "app.agent.harness.task_progress.SnapshotStore",
        lambda: native_file_context["snapshot_store"],
    )
    return native_file_context


def outcome(identity, path):
    """Describe one explicit file outcome before its tool operation executes."""
    return {
        "id": identity,
        "description": f"Update {identity}",
        "criteria": "Save the requested content",
        "kind": "file_change",
        "targets": [str(path)],
    }


def record_write(progress, context, path, content, *, claimed_path=None):
    """Return task evidence from a real native file write and durable verifier."""
    operation = _start_operation(context["run"])
    output = _invoke_file_write(operation, path=path, content=content, overwrite=True)
    operation.settle(json.dumps(output))
    return progress.record(
        SimpleNamespace(
            call_id=operation.operation_id,
            tool_name="fs_write",
            operation_id=operation.operation_id,
            arguments={"path": str(claimed_path or path)},
        ),
        ToolCallResult.from_output(
            call_id=operation.operation_id,
            name="fs_write",
            output=json.dumps(output),
        ),
        context["run"],
    )


def test_task_retains_all_outcomes_and_verified_evidence_after_database_reopen(native_context):
    """A third unfinished file survives successful earlier batches and restart."""
    context = native_context
    context["set_policy"](mode="full_access")
    run, root, db = context["run"], context["root"], context["db"]
    progress = TaskProgress(db, "chat-native-file-integration", "Update three files", str(root))
    steps = [outcome(name, root / f"{name}.txt") for name in ("first", "second", "third")]
    progress.update(steps)
    for index in range(2):
        target = root / f"{steps[index]['id']}.txt"
        operation = _start_operation(run)
        output = _invoke_file_write(operation, path=target, content="updated", overwrite=True)
        operation.settle(json.dumps(output))
        evidence = progress.record(
            SimpleNamespace(
                call_id=f"write-{index}",
                tool_name="fs_write",
                operation_id=operation.operation_id,
                arguments={"path": str(target)},
            ),
            ToolCallResult.from_output(call_id=f"write-{index}", name="fs_write", output=json.dumps(output)),
            run,
        )
        assert evidence["verified_file_change"] is True
        progress.update([{**steps[index], "status": "done", "evidence_ids": [evidence["id"]]}])
    progress.update([{**steps[2], "status": "failed", "reason": "Missing source information"}])
    assert "third" in progress.completion_blocker()
    reopened = Database(root.parent / "agent.db")
    reopened.init_db()
    try:
        restored = TaskProgress(
            reopened, "chat-native-file-integration", "Continue", str(root), continuation=True
        )
        assert restored.data["goal"] == "Update three files"
        assert [step["status"] for step in restored.data["steps"]] == ["done", "done", "failed"]
        assert len(restored.data["evidence"]) == 2
        assert "third" in restored.context()
    finally:
        reopened.close()


def test_output_cannot_forge_verified_evidence_or_drop_pending_targets(native_context):
    """An ok JSON response and a guessed ID cannot prove a file mutation."""
    root = native_context["root"]
    progress = TaskProgress(native_context["db"], "chat-native-file-integration", "Update files", str(root))
    step = outcome("both", root / "a.txt")
    step["targets"].append(str(root / "b.txt"))
    progress.update([step])
    evidence = progress.record(
        SimpleNamespace(
            call_id="fake", tool_name="fs_write", operation_id="", arguments={"path": str(root / "a.txt")}
        ),
        ToolCallResult.from_output(
            call_id="fake", name="fs_write", output='{"status":"ok","verification_result":"completed"}'
        ),
        None,
    )
    assert evidence["verified_file_change"] is False
    with pytest.raises(ValueError, match="durable verification"):
        progress.update([{**step, "status": "done", "evidence_ids": [evidence["id"]]}])
    with pytest.raises(ValueError, match="recorded evidence"):
        progress.update([{**step, "status": "done", "evidence_ids": ["invented"]}])
    progress.update([], status="waiting_user", reason="Which source should be used?")
    assert progress.update([])["status"] == "waiting_user"
    progress.finish_answer("Which source should be used?")
    assert progress.data["status"] == "waiting_user"
    assert progress.data["steps"][0]["status"] == "pending"


def test_later_file_changes_invalidate_completion_even_after_evidence_pruning(native_context):
    """Neither omitted outcomes nor old pinned evidence can hide a later write."""
    context = native_context
    context["set_policy"](mode="full_access")
    target = context["root"] / "result.txt"
    progress = TaskProgress(
        context["db"], "chat-native-file-integration", "Edit result", str(context["root"])
    )
    progress.update([outcome("result", target)])
    first = record_write(progress, context, target, "first", claimed_path=context["root"] / "wrong.txt")
    progress.update([{"id": "result", "status": "done", "evidence_ids": [first["id"]]}])
    second = record_write(progress, context, target, "second")
    assert progress.data["status"] == "working"
    assert progress.data["steps"][0]["status"] == "pending"
    assert progress.data["evidence"][-1]["paths"] == [progress.path_key(str(target))]
    for index in range(65):
        progress.record(
            SimpleNamespace(call_id=str(index), tool_name="lookup", operation_id="", arguments={}),
            ToolCallResult.from_output(call_id=str(index), name="lookup", output='{"status":"ok"}'),
            None,
        )
    assert {first["id"], second["id"]} <= {item["id"] for item in progress.data["evidence"]}
    with pytest.raises(ValueError, match="durable verification"):
        progress.update([{"id": "result", "status": "done", "evidence_ids": [first["id"]]}])
    progress.update([{"id": "result", "status": "done", "evidence_ids": [second["id"]]}])
    assert progress.data["status"] == "complete"


@pytest.mark.parametrize("replace_task", [False, True])
def test_stale_task_writers_cannot_overwrite_or_reactivate_old_state(native_context, replace_task):
    """Fence both concurrent revisions and a newly selected active task."""
    context = native_context
    args = context["db"], "chat-native-file-integration", "Original task", str(context["root"])
    first = TaskProgress(*args)
    first.update([outcome("original", "original.txt")])
    stale, current = TaskProgress(*args), TaskProgress(*args)
    current.update([outcome("next", "next.txt")] if replace_task else [], new_task=replace_task)
    with pytest.raises(RuntimeError, match="changed; reload"):
        stale.update([], status="blocked", reason="Stale writer")
    restored = TaskProgress(*args)
    assert restored.snapshot() == current.snapshot()
    assert restored.data["status"] == "working"


def test_misclassified_read_check_can_be_corrected_without_changing_its_outcome(native_context):
    """A planning mistake must not force another file edit to manufacture proof."""
    context = native_context
    progress = TaskProgress(
        context["db"], "chat-native-file-integration", "Read result", str(context["root"])
    )
    step = {
        **outcome("read", "result.txt"),
        "description": "Read the result",
        "criteria": "Check the saved value",
    }
    progress.update([step])
    evidence = progress.record(
        SimpleNamespace(
            call_id="read", tool_name="fs_read", operation_id="", arguments={"path": "result.txt"}
        ),
        ToolCallResult.from_output(call_id="read", name="fs_read", output='{"status":"ok","content":"7"}'),
        None,
    )
    with pytest.raises(ValueError, match="with a reason"):
        progress.update([{"id": "read", "kind": "general"}])
    progress.update(
        [
            {
                "id": "read",
                "kind": "general",
                "status": "done",
                "evidence_ids": [evidence["id"]],
                "reason": "This is a read-back check, not another edit.",
            }
        ]
    )
    saved = progress.data["steps"][0]
    assert saved["criteria"] == step["criteria"] and saved["targets"] == step["targets"]
    assert saved["kind_corrections"][0]["from"] == "file_change"
    assert progress.data["status"] == "complete"
    assert all(not item["verified"] for item in progress.data["evidence"])


@pytest.mark.parametrize("polling", [False, True])
def test_unchanged_observations_warn_then_pause_with_bounded_polling(polling):
    """Transport timestamps and alternating reads cannot evade the stall limit."""
    monitor = ProgressMonitor()
    tool = {"metadata": {"observation": True, "progress_polling": polling}}
    pauses, warnings = [], []
    for index in range(16):
        monitor.begin_round()
        monitor.observe(
            "read", {"path": str(index % 2)}, json.dumps({"value": "same", "timestamp": index}), "ok", tool
        )
        hint, paused = monitor.finish_round()
        if hint and not paused:
            warnings.append(index)
        if paused:
            pauses.append(index)
            break
    assert len(warnings) == 1
    assert pauses == ([13] if polling else [7])
    monitor.begin_round()
    monitor.observe("read", {"path": "0"}, '{"value":"changed"}', "ok", tool)
    assert monitor.finish_round() == ("", False)
    assert monitor.stalled_rounds == 0
    repeats = monitor.redundant_calls
    monitor.steer()
    assert not monitor.seen and monitor.redundant_calls == repeats


def test_coordinator_cannot_finish_a_task_with_unresolved_outcomes(native_context):
    """The actual loop gives one repair opportunity and keeps the task resumable."""
    context = native_context

    class Memory(PersistingFakeMemory):
        """Use the real durable ledger with the small transcript fixture."""

        def open_task_progress(self, conversation_id, goal, workspace, **kwargs):
            """Bind the coordinator's planning tool to the isolated database."""
            return TaskProgress(
                context["db"], "chat-native-file-integration", goal, str(context["root"]), **kwargs
            )

    class Model:
        """Deliberately try to finish after declaring unfinished work."""

        calls = 0

        async def chat_with_tools(self, messages, tools, **kwargs):
            """Declare an outcome, then exercise both completion checks."""
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(
                            "plan",
                            "task_plan",
                            {
                                "steps": [outcome("file", context["root"] / "pending.txt")],
                            },
                        )
                    ],
                )
            return LLMResponse(content="All done.")

    model, memory = Model(), Memory()
    loop = TurnLoop(model, ToolRegistry(), memory, observability=FakeObservability())

    async def collect():
        """Run the same coordinator used by the app."""
        return [
            event
            async for event in loop.run("Update the file", "chat-native-file-integration", system_prompt="")
        ]

    try:
        events = asyncio.run(collect())
    finally:
        loop.shutdown()
    assert model.calls == 3
    assert events[-1]["data"]["reason_code"] == "task_outcomes_incomplete"
    assert memory.clear_task_progress_calls == 0
