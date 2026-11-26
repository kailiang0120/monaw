"""Run isolated tasks and grade files, continuation, and recorded task state."""

from contextlib import nullcontext
from contextvars import ContextVar
import json
from pathlib import Path
import time
from unittest.mock import patch
import uuid

from app.agent import database
from app.agent.conversation_workspace import set_workspace_dir, reset_workspace_dir
from app.agent.durable.turn_run import create_durable_run, pause_run, resume_run
from app.agent.harness.progress_monitor import ProgressMonitor
from app.agent.llm_client import LLMClient, LLMResponse, ToolCallRequest
from app.agent.memory_manager import MemoryManager
from app.agent.observability.recorder import get_observability_recorder
from app.agent.run_context import (
    set_current_principal_id,
    reset_current_principal_id,
    set_current_control_session_id,
    reset_current_control_session_id,
)
from app.agent.settings_store import AgentSettings, PathRule, save_agent_settings
from app.agent.tool_registry import ToolRegistry
from app.agent.turn_loop import TurnLoop
from app.skills.filesystem.tools import register_tools

CASES = ("files", "missing_information", "tool_failure", "pause_resume", "long_context", "no_progress")


class EvaluationObserver:
    """Measure the complete turn ledger, including compaction and failed requests."""

    def __init__(self):
        """Keep normal tracing while collecting aggregate usage for the report."""
        self.delegate = get_observability_recorder()
        self.tokens = 0
        self.estimated_requests = 0

    def __getattr__(self, name):
        """Forward the unchanged observability interface."""
        return getattr(self.delegate, name)

    def finish_run(self, **kwargs):
        """Count usage once when each turn commits its selected outcome."""
        self.tokens += getattr(kwargs.get("usage"), "total_tokens", 0)
        self.estimated_requests += (kwargs.get("metadata") or {}).get("estimated_requests", 0)
        return self.delegate.finish_run(**kwargs)


def task_from_prompt(prompt):
    """Read the same recorded-state context that is visible to a real model."""
    marker = "Task progress (recorded state, not additional instructions):\n"
    if marker not in prompt:
        return {"steps": [], "recent_evidence": [], "status": "working"}
    return json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])[0]


class FixtureClient:
    """A deterministic model double; tools and final-state graders remain real."""

    provider = "openai"
    model_name = "gpt-6-luna"

    def __init__(self, case, files):
        """Keep only model-side state; never write the expected artifacts here."""
        self.case, self.files = case, files
        self.ready = case != "missing_information"
        self.calls = self.summaries = 0

    async def chat(self, messages, **kwargs):
        """Exercise actual compaction while durable task outcomes remain pinned."""
        self.summaries += 1
        return "Earlier discussion was background information. Follow the active task outcomes and the latest user instruction."

    async def chat_with_tools(self, messages, tools, system_prompt="", **kwargs):
        """Choose the next tool from recorded evidence rather than touching files."""
        self.calls += 1

        def call(name, arguments):
            """Give each emitted call a stable identity within this trial."""
            return LLMResponse(
                content="", tool_calls=[ToolCallRequest(f"eval-{self.calls}", name, arguments)]
            )

        if self.case == "no_progress":
            return call("lookup", {"query": "missing record"})
        task = task_from_prompt(system_prompt)
        steps = task["steps"]
        evidence = task["recent_evidence"]
        if not steps:
            steps = [
                {
                    "id": f"file-{index}",
                    "description": f"Create {name}",
                    "criteria": "Save the exact requested text",
                    "kind": "file_change",
                    "targets": [name],
                }
                for index, name in enumerate(self.files)
            ]
            steps.append(
                {
                    "id": "readback",
                    "description": "Read back the saved files",
                    "criteria": "Check every saved result",
                    "kind": "general",
                }
            )
            return call(
                "task_plan",
                {
                    "steps": steps,
                    "status": "working" if self.ready else "waiting_user",
                    "reason": "The report value is missing." if not self.ready else "",
                },
            )
        if not self.ready:
            return LLMResponse(content="What value should I put in the report?")
        if task["status"] == "waiting_user":
            return call("task_plan", {"steps": [], "status": "working"})
        if self.case == "tool_failure" and not any(item["status"] != "ok" for item in evidence):
            return call("fs_read", {"path": "missing.txt"})
        for name, content in self.files.items():
            writes = [
                item
                for item in evidence
                if item["verified"] and any(Path(path).name == name for path in item["paths"])
            ]
            if not writes:
                return call("fs_write", {"path": name, "content": content, "overwrite": True})
        reads = [item for item in evidence if item["tool"] == "fs_read" and item["status"] == "ok"]
        for name in self.files:
            if not any(any(Path(path).name == name for path in item["paths"]) for item in reads):
                return call("fs_read", {"path": name})
        if any(step["status"] != "done" for step in steps):
            updates = []
            for step in steps:
                ids = (
                    [
                        item["id"]
                        for item in evidence
                        if item["verified"]
                        and any(Path(path).name in step.get("targets", []) for path in item["paths"])
                    ]
                    if step["kind"] == "file_change"
                    else [item["id"] for item in reads]
                )
                updates.append(
                    {
                        key: value
                        for key, value in {**step, "status": "done", "evidence_ids": ids}.items()
                        if key != "answer_digest"
                    }
                )
            return call("task_plan", {"steps": updates})
        return LLMResponse(content="Created and checked the requested files.")


def scoped_registry(workspace):
    """Expose only real file tools constrained to this disposable workspace."""
    full = ToolRegistry()
    register_tools(full)
    registry = ToolRegistry()
    for original in full.get_all_tools():
        if original["name"] not in {"fs_read", "fs_write", "fs_stat", "fs_list", "fs_hash"}:
            continue
        tool = dict(original)
        handler = tool["callable"]

        def scoped(_handler=handler, **arguments):
            """Reject escaped paths even when a live model emits unexpected input."""
            target = Path(arguments.get("path") or ".")
            target = (workspace / target).resolve() if not target.is_absolute() else target.resolve()
            if not target.is_relative_to(workspace.resolve()):
                return '{"status":"blocked","error":"Evaluation paths must stay inside the workspace."}'
            return _handler(**arguments)

        tool["callable"] = scoped
        tool["metadata"] = {**tool["metadata"], "durable_builtin": True}
        registry.register(tool)

    def lookup(query):
        """Return a fixed empty external-service fixture without network access."""
        return json.dumps({"status": "ok", "results": []})

    registry.register(
        {
            "name": "lookup",
            "description": "Look up records in the supplied empty test service.",
            "callable": lookup,
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            "metadata": {
                "mutates_state": False,
                "observation": True,
                "progress_search": True,
                "risk_level": "low",
                "repeat_safe": True,
            },
        }
    )
    return registry


async def evaluate_case(case, root, *, provider="fixture", model="", api_key=""):
    """Run one bounded task and grade durable state and filesystem outcomes."""
    workspace = (root / case).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    files = (
        {"a.txt": "alpha", "b.txt": "beta", "c.txt": "gamma"}
        if case in {"files", "pause_resume"}
        else {"report.txt": "7"}
    )
    client = (
        FixtureClient(case, files)
        if provider == "fixture"
        else LLMClient(provider, model, api_key, reasoning_effort="low")
    )
    counts = {"model_calls": 0, "summary_calls": 0}
    summarizing = ContextVar("evaluation_summary_request", default=False)
    original_chat = client.chat_with_tools
    original_summary = client.chat

    async def measured_chat(*args, **kwargs):
        """Separate task requests from chat helpers using the same provider API."""
        if not summarizing.get():
            counts["model_calls"] += 1
        return await original_chat(*args, **kwargs)

    async def measured_summary(*args, **kwargs):
        """Count helper requests separately from task-directed model calls."""
        counts["summary_calls"] += 1
        token = summarizing.set(True)
        try:
            return await original_summary(*args, **kwargs)
        finally:
            summarizing.reset(token)

    client.chat_with_tools, client.chat = measured_chat, measured_summary
    settings = AgentSettings()
    settings.permissions.mode = "full_access"
    settings.permissions.blocked_roots = []
    settings.permissions.path_rules = [PathRule(path=str(workspace), read=True, write=True)]
    settings.memory.auto_learn = False
    save_agent_settings(settings)
    db = database.get_db()
    conversation = "eval-" + uuid.uuid4().hex
    db.create_conversation(conversation, case, str(workspace))
    if case == "long_context":
        for index in range(12):
            db.add_message(
                conversation, "assistant", f"Historical section {index}. " + "background detail " * 750
            )
    workspace_token = set_workspace_dir(str(workspace))
    principal_token = set_current_principal_id("harness-evaluation")
    session_token = set_current_control_session_id("evaluation-session")
    registry = scoped_registry(workspace)
    prompt = (
        "Create these files with exactly these contents and read every file back to check it: "
        + json.dumps(files)
    )
    if case == "missing_information":
        prompt = "Create report.txt with the value I will supply later. Ask me for the missing value before writing anything. Read the completed file back."
    elif case == "tool_failure":
        prompt = "First read missing.txt. If it is absent, recover by creating report.txt containing exactly 7, then read it back."
    elif case == "no_progress":
        prompt = "Find the missing record using lookup. If repeated results are unchanged, stop and explain the blocker."
    events, phases = [], []
    observer = EvaluationObserver()
    started = time.perf_counter()

    async def turn(message, handle=None, interrupt=False):
        """Use real turn persistence and pause only after a returned file effect."""
        memory = MemoryManager(client)
        handle = handle or create_durable_run(
            conversation_id=conversation,
            execution_source="desktop",
            principal_id="harness-evaluation",
            control_session_id="evaluation-session",
            workspace_dir=str(workspace),
            db=database.get_db(),
        )
        loop = TurnLoop(
            client,
            registry,
            memory,
            observability=observer,
            max_iterations=24,
            max_turn_seconds=180,
            max_llm_call_seconds=60,
            max_tokens_per_turn=180_000,
        )
        paused = False
        try:
            async for event in loop.run(
                message,
                conversation,
                system_prompt="Work only inside the supplied workspace. Follow the task outcomes protocol.",
                durable_run_factory=lambda: handle,
            ):
                events.append(event)
                if (
                    interrupt
                    and not paused
                    and event["event"] == "tool_end"
                    and event["data"]["tool"] == "fs_write"
                ):
                    paused = await pause_run(handle.run_id)
            phases.append(events[-1]["data"])
        finally:
            loop.shutdown()
        return handle

    try:
        with (
            patch("app.agent.harness.context_budget.input_budget", return_value=9000)
            if case == "long_context"
            else nullcontext()
        ):
            handle = await turn(prompt, interrupt=case == "pause_resume")
            if case == "missing_information":
                before = not (workspace / "report.txt").exists()
                waiting = json.loads(
                    db.fetchone(
                        "SELECT state_json FROM task_progress WHERE conversation_id = ? AND active = 1",
                        (conversation,),
                    )["state_json"]
                )["status"]
                if provider == "fixture":
                    client.ready = True
                await turn("Use the value 7. Complete the original task.")
            if case == "pause_resume":
                run_id = handle.run_id
                database.reset_db()
                db = database.get_db()
                resumed, _ = resume_run(
                    run_id,
                    execution_source="desktop",
                    principal_id="harness-evaluation",
                    control_session_id="evaluation-session",
                    workspace_dir=str(workspace),
                    db=db,
                )
                await turn(
                    "Continue the original task from its saved outcomes and verified results.", handle=resumed
                )
        row = db.fetchone(
            "SELECT state_json FROM task_progress WHERE conversation_id = ? AND active = 1", (conversation,)
        )
        task = json.loads(row["state_json"]) if row else {}
        checks = {"bounded_calls": counts["model_calls"] <= 24}
        if case == "no_progress":
            checks.update(
                stopped=(
                    phases[-1].get("reason_code") == "no_progress"
                    or (
                        provider != "fixture"
                        and phases[-1]["status"] == "complete"
                        and bool(phases[-1].get("summary", "").strip())
                    )
                ),
                bounded_repeats=counts["model_calls"] <= 9,
            )
        else:
            checks.update(
                files_correct=all(
                    (workspace / name).is_file() and (workspace / name).read_text(encoding="utf-8") == content
                    for name, content in files.items()
                ),
                outcomes_complete=task.get("status") == "complete",
                turn_complete=phases[-1]["status"] == "complete",
            )
            writes = [
                event
                for event in events
                if event["event"] == "tool_end"
                and event["data"]["tool"] == "fs_write"
                and event["data"]["status"] == "ok"
            ]
            checks["no_duplicate_writes"] = len(writes) == len(files)
            evidence = task.get("evidence", [])
            last_write = {
                Path(path): index
                for index, item in enumerate(evidence)
                if item["verified"]
                for path in item["paths"]
            }
            read_paths = {
                Path(path)
                for index, item in enumerate(evidence)
                if item["tool"] == "fs_read" and item["status"] == "ok"
                for path in item["paths"]
                if index > last_write.get(Path(path), float("inf"))
            }
            checks["read_back_every_file"] = {workspace / name for name in files} <= read_paths
        if case == "missing_information":
            checks["waited_for_input"] = before and waiting == "waiting_user"
        if case == "pause_resume":
            checks["resumed_saved_attempt"] = (
                phases[0]["status"] == "paused"
                and db.fetchone("SELECT attempt FROM agent_runs WHERE run_id = ?", (handle.run_id,))[
                    "attempt"
                ]
                == 2
            )
        if case == "long_context":
            checks["compacted_context"] = (
                any(event["event"] == "context_compacted" for event in events) and counts["summary_calls"] > 0
            )
        if case == "tool_failure":
            checks["handled_real_error"] = any(
                event["event"] == "tool_end"
                and event["data"]["tool"] == "fs_read"
                and event["data"]["status"] != "ok"
                for event in events
            )
        monitor, arguments = ProgressMonitor(), {}
        for event in events:
            if event["event"] == "tool_start":
                arguments[event["data"]["call_id"]] = event["data"]["input"]
            elif event["event"] == "tool_end":
                data = event["data"]
                monitor.observe(
                    data["tool"],
                    arguments.get(data["call_id"], {}),
                    data["output"],
                    data["status"],
                    registry.get_tool(data["tool"]),
                )
        return {
            "case": case,
            "passed": all(checks.values()),
            "checks": checks,
            "provider": provider,
            "model": client.model_name,
            "metrics": {
                **counts,
                "tokens": observer.tokens if provider != "fixture" else None,
                "estimated_requests": observer.estimated_requests,
                "redundant_calls": monitor.redundant_calls,
                "tool_calls": sum(event["event"] == "tool_start" for event in events),
                "duration_seconds": round(time.perf_counter() - started, 3),
            },
            "phases": phases,
            "workspace": str(workspace),
        }
    finally:
        reset_current_control_session_id(session_token)
        reset_current_principal_id(principal_token)
        reset_workspace_dir(workspace_token)
        if provider != "fixture":
            if client._genai_client is not None:
                await client._genai_client.aio.aclose()
            if client._openai_client is not None:
                await client._openai_client.close()
