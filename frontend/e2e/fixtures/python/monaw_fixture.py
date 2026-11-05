"""Controlled provider and barrier tool; production auth, execution and persistence stay real."""
import asyncio
import json
import os
from pathlib import Path
import re
import importlib.abc
import importlib.machinery
import sys
from functools import wraps
import threading
import time
from datetime import timedelta

ROOT = Path(os.environ["MONAW_E2E_ROOT"]).resolve()
CONTROL = ROOT / "control"
CONTROL.mkdir(exist_ok=True)
_provider_call_lock = threading.Lock()


async def barrier(name: str) -> None:
    if not re.fullmatch(r"[a-z-]+", name):
        raise ValueError("Invalid fixture barrier")
    (CONTROL / f"{name}.started").touch()
    while not (CONTROL / f"{name}.release").exists():
        await asyncio.sleep(0.05)


def record(kind: str, **fields) -> None:
    with (CONTROL / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": kind, **fields}) + "\n")


class FixtureProvider:
    def __init__(self, provider: str):
        self.provider = provider
        self.calls = {}

    def _next_call_index(self, conversation: str, scenario: str) -> int:
        if scenario not in {"file-resume", "file-conflict", "opaque-resume"}:
            key = (conversation, scenario)
            index = self.calls.get(key, 0)
            self.calls[key] = index + 1
            return index

        # These workflows deliberately span backend (and Electron) restarts.
        # Keep only the synthetic provider call count in the disposable fixture
        # so a restarted fake provider can advance to the next scripted step.
        if not re.fullmatch(r"[a-z0-9_-]{1,128}", conversation):
            raise RuntimeError("Invalid fixture conversation identifier")
        call_file = CONTROL / f"provider-{scenario}-{conversation}.count"
        with _provider_call_lock:
            try:
                index = int(call_file.read_text(encoding="ascii"))
            except FileNotFoundError:
                index = 0
            except (OSError, ValueError) as exc:
                raise RuntimeError("Invalid persistent fixture provider count") from exc
            call_file.write_text(str(index + 1), encoding="ascii")
            return index

    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None,
                              tool_choice=None, **kwargs):
        from app.agent.llm_client import LLMResponse, ToolCallRequest
        from app.agent.run_context import current_conversation_id

        if not tools:
            return LLMResponse(content="Fixture auxiliary response")
        markers = re.findall(r"\[e2e:([a-z-]+)\]", json.dumps(messages))
        if not markers:
            raise RuntimeError("Unscripted provider request in application test")
        scenario = markers[-1]
        conversation = current_conversation_id()
        index = self._next_call_index(conversation, scenario)
        record("model", scenario=scenario, conversation=conversation, index=index)

        def call(name, arguments, suffix=""):
            return ToolCallRequest(call_id=f"fixture-{scenario}-{index}{suffix}", tool_name=name, arguments=arguments)

        answer = f"Fixture {scenario} completed."
        if index == 0:
            if scenario == "stream":
                if stream_callback:
                    await stream_callback("Fixture stream started. ")
                await barrier("stream")
                answer = "Fixture stream started. Fixture stream completed."
            elif scenario in {"allow", "deny"}:
                return LLMResponse(content="", tool_calls=[call("fs_append", {
                    "path": str(ROOT / "workspace" / f"{scenario}.txt"), "content": "one change\n",
                })], finish_reason="tool_calls")
            elif scenario in {"file-resume", "file-conflict"}:
                return LLMResponse(content="", tool_calls=[call("fs_write", {
                    "path": str(ROOT / "workspace" / f"{scenario}.txt"),
                    "content": "approved update\n",
                    "overwrite": True,
                    "create_parents": False,
                })], finish_reason="tool_calls")
            elif scenario == "opaque-resume":
                return LLMResponse(content="", tool_calls=[call("exec", {
                    "command": "Write-Output 'fixture opaque action'", "shell": "powershell",
                })], finish_reason="tool_calls")
            elif scenario == "exact-action":
                return LLMResponse(content="", tool_calls=[call("e2e_exact_action", {})], finish_reason="tool_calls")
            elif scenario == "parallel":
                return LLMResponse(content="", tool_calls=[
                    call("fs_read", {"path": str(ROOT / "workspace" / "read.txt")}, "-read"),
                    call("e2e_barrier", {"name": "parallel"}, "-wait"),
                ], finish_reason="tool_calls")
            elif scenario == "interrupt":
                return LLMResponse(content="", tool_calls=[call("fs_append", {
                    "path": str(ROOT / "workspace" / "interrupt.txt"), "content": "one change\n",
                })], finish_reason="tool_calls")
            elif scenario == "schedule":
                if stream_callback:
                    await stream_callback(f"Fixture {scenario} started. ")
                await barrier(scenario)
            elif scenario in {"mcp", "mcp-schema"}:
                if scenario == "mcp-schema":
                    record("mcp_schema", schema_has_extra=any('get_credential_status' in str(tool) and 'extra' in str(tool) for tool in tools))
                return LLMResponse(content="", tool_calls=[call("mcp__fixture__get_credential_status",
                    {"label": "fixture", **({"extra": "new schema"} if scenario == "mcp-schema" else {})})], finish_reason="tool_calls")
            elif scenario != "recover":
                raise RuntimeError(f"Unknown application fixture: {scenario}")
        if scenario == "interrupt" and index == 1:
            if stream_callback:
                await stream_callback("Fixture interrupt started. ")
            await barrier("interrupt")
        if scenario in {"file-resume", "opaque-resume"} and index == 1:
            await barrier(scenario)
        # Opt-in: hold the resumed call so a test can watch the continued Run live.
        if scenario == "file-resume" and index == 2 and (CONTROL / "file-resume-continue.hold").exists():
            await barrier("file-resume-continue")
        return LLMResponse(content="", tool_calls=[call("final_answer", {"answer": answer})], finish_reason="tool_calls")


def configure_runtime(runtime) -> None:
    original = runtime.load_tools

    def exact_action() -> str:
        from app.agent.action_security import attest_action, authorize_action
        action = attest_action(tool_name="e2e_exact_action", action_kind="upload", parameters={},
            target={"origin": "https://fixture.example", "page_id": 7}, destination="https://fixture.example/upload",
            context_version="fixture-document-1", side_effect="external_mutation",
            data_scope={"file_count": 1, "size_bytes": 12, "files": [{"name": "report.pdf", "size_bytes": 12}]},
            runtime_facts={"target_verified": True, "context_verified": True, "scope_allowed": True})
        stopped = authorize_action(action)
        if stopped is not None:
            return json.dumps(stopped)
        # The external side effect is replaced with a disposable fixture counter;
        # policy, approval routes, exact replay, journal and UI remain production.
        record("exact_action_dispatched")
        return json.dumps({"status": "ok"})

    async def wait_tool(name: str) -> str:
        if name == "parallel":
            while not (CONTROL / "read.completed").exists():
                await asyncio.sleep(0.01)
        await barrier(name)
        return json.dumps({"status": "ok", "result": "Fixture barrier released"})

    def load_tools(*args, **kwargs):
        skills, registry = original(*args, **kwargs)
        from app.agent.execution_resume import register_executor
        register_executor("e2e_exact_action", lambda _input: exact_action())
        reader = registry.get_tool("fs_read")
        if reader:
            original_reader = reader["callable"]
            @wraps(original_reader)
            def read(*args, **kwargs):
                result = original_reader(*args, **kwargs)
                (CONTROL / "read.completed").touch()
                return result
            reader["callable"] = read
        registry.extend([{
            "name": "e2e_exact_action", "description": "Controlled consequential action",
            "parameters": {"type": "object", "properties": {}}, "callable": exact_action,
            "metadata": {"parallel_safe": False, "mutates_state": True, "risk_level": "high"},
        }, {
            "name": "e2e_barrier", "description": "Controlled application-test barrier",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            "callable": wait_tool,
            "execution_mode": "async",
            "metadata": {"parallel_safe": True, "resource_locks": [], "mutates_state": False, "risk_level": "low"},
        }])
        return skills, registry

    runtime.load_tools = load_tools


def install() -> None:
    # Wait for normal imports: sitecustomize runs before Python's application
    # search path and entry-point initialization are complete.
    class FixtureLoader(importlib.abc.Loader):
        def __init__(self, loader):
            self.loader = loader

        def create_module(self, spec):
            return self.loader.create_module(spec)

        def exec_module(self, module):
            self.loader.exec_module(module)
            if module.__name__ == "app.agent.llm_provider_adapters":
                module.create_llm_provider_adapter = lambda provider, _client: FixtureProvider(provider)
            elif module.__name__ == "app.agent.runtime":
                configure_runtime(module)
            elif module.__name__ == "app.api.routes.diagnostics":
                # Setup may prepare files in the disposable home, but must not
                # open Explorer or Chrome while exercising the Copy button.
                module._open_folder = lambda _folder: False
                module._open_extensions_page = lambda: False
            else:
                original_now = module._utc_now
                module._utc_now = lambda: original_now() + timedelta(seconds=float(
                    (CONTROL / "clock-offset").read_text() if (CONTROL / "clock-offset").exists() else "0"))
                service = module.ScheduledTaskService
                original_init = service.__init__
                def init(self, *args, **kwargs):
                    kwargs.setdefault("tick_interval", 0.1)
                    original_init(self, *args, **kwargs)
                service.__init__ = init
                original_fire = service._fire_due_tasks
                async def fire(self):
                    await original_fire(self)
                    prior = int((CONTROL / "scheduler-ticks").read_text()) if (CONTROL / "scheduler-ticks").exists() else 0
                    (CONTROL / "scheduler-ticks").write_text(str(prior + 1))
                service._fire_due_tasks = fire

    class FixtureImports(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname not in {"app.agent.llm_provider_adapters", "app.agent.runtime", "app.agent.scheduler", "app.api.routes.diagnostics"}:
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec is not None and spec.loader is not None:
                spec.loader = FixtureLoader(spec.loader)
            return spec

    sys.meta_path.insert(0, FixtureImports())
    def restart_watcher():
        while True:
            if (CONTROL / "restart.request").exists():
                (CONTROL / "restart.request").unlink()
                record("restart")
                os._exit(78)  # Exercise Electron's existing backend-restart path.
            time.sleep(0.05)
    threading.Thread(target=restart_watcher, daemon=True).start()
    record("backend_started", pid=os.getpid())
    (CONTROL / "provider-installed").touch()
