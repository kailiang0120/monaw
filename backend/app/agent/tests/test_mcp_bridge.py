import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from app.agent.approval_broker import approve_ticket, get_ticket
from app.agent.execution_resume import resume_approved_ticket
from app.agent.settings_store import MCPServerConfig
from app.agent.tool_registry import ToolRegistry
from app.skills.mcp_bridge import tools as mcp_tools_module
from app.skills.mcp_bridge.connection import ServerManager
from app.skills.mcp_bridge.registry import (
    build_tool_entries,
    clear_all_reflected_tools,
    clear_reflected_tools_for_server,
    get_reflected_tool_map,
    reflected_tool_name,
)
from app.skills.mcp_bridge.tools import register_tools


class _FakeSession:
    async def call_tool(self, name: str, arguments: dict):
        return SimpleNamespace(
            isError=False,
            structuredContent={"tool": name, "arguments": arguments},
            content=[SimpleNamespace(type="text", text=f"ran {name}")],
        )

    async def list_tools(self):
        return SimpleNamespace(tools=[])


class _SlowSession:
    async def call_tool(self, name: str, arguments: dict):  # noqa: ARG002
        await asyncio.sleep(999)


@pytest.fixture(autouse=True)
def _clear_reflected_tools():
    clear_all_reflected_tools()
    yield
    clear_all_reflected_tools()


def _run_loop(loop: asyncio.AbstractEventLoop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _settings_with_servers(servers: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(mcp=SimpleNamespace(servers=servers))


def test_reflected_tool_name_sanitizes_dedupes_and_caps_length():
    used: set[str] = set()

    first = reflected_tool_name("bad server!", "list directory/items", used=used)
    second = reflected_tool_name("bad server!", "list directory/items", used=used)
    long_name = reflected_tool_name("filesystem", "x" * 100, used=used)

    assert first == "mcp__bad_server__list_directory_items"
    assert second == "mcp__bad_server__list_directory_items__2"
    assert len(long_name) <= 64


def test_build_tool_entries_prefixes_names_uses_affinity_and_maps_original_tool():
    cfg = MCPServerConfig(name="filesystem", command="npx", trusted_tools=["list_directory"])
    manager = SimpleNamespace(
        reflected_tool_names=[],
        call_tool_sync=lambda tool_name, arguments: json.dumps({"tool": tool_name, "arguments": arguments}),
    )
    tool = SimpleNamespace(
        name="list_directory",
        description="List directory contents",
        inputSchema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )

    entries = build_tool_entries(cfg, manager, [tool])

    assert len(entries) == 1
    assert entries[0]["name"] == "mcp__filesystem__list_directory"
    assert entries[0]["execution_mode"] == "sync_thread_affine"
    assert entries[0]["affinity_group"] == "mcp:filesystem"
    assert entries[0]["parameters"]["required"] == ["path"]
    assert entries[0]["mcp_bridge"]["approval"]["reason"] == "explicitly_trusted_tool"
    assert entries[0]["mcp_bridge"]["schema_hash"]
    assert json.loads(entries[0]["callable"](path=r"C:\Repo"))["tool"] == "list_directory"
    assert get_reflected_tool_map()["mcp__filesystem__list_directory"]["original_tool_name"] == "list_directory"
    assert manager.reflected_tool_names == ["mcp__filesystem__list_directory"]


def test_read_like_mcp_tool_requires_approval_without_explicit_trust():
    cfg = MCPServerConfig(name="filesystem", command="npx")
    manager = SimpleNamespace(reflected_tool_names=[], call_tool_sync=lambda _tool, _args: "{}")
    tool = SimpleNamespace(
        name="list_directory",
        description="List directory contents",
        inputSchema={"type": "object", "properties": {"path": {"type": "string"}}},
        annotations=SimpleNamespace(readOnlyHint=True),
    )

    entry = build_tool_entries(cfg, manager, [tool])[0]
    pending = json.loads(entry["callable"](path=r"C:\Repo"))

    assert pending["status"] == "pending_approval"
    assert pending["reason"] == "untrusted_read_tool"
    assert pending["schema_hash"] == entry["mcp_bridge"]["schema_hash"]


def test_unknown_reflected_tool_requires_approval_and_resumes_original_call():
    calls: list[tuple[str, dict]] = []
    cfg = MCPServerConfig(name="remote", command="npx")
    manager = SimpleNamespace(
        reflected_tool_names=[],
        call_tool_sync=lambda tool_name, arguments: (
            calls.append((tool_name, arguments)),
            json.dumps({"ok": True, "tool": tool_name, "arguments": arguments}),
        )[1],
    )
    tool = SimpleNamespace(
        name="transform",
        description="Transform data",
        inputSchema={"type": "object", "properties": {"value": {"type": "string"}}},
    )

    entry = build_tool_entries(cfg, manager, [tool])[0]
    pending = json.loads(entry["callable"](value="abc"))

    assert pending["status"] == "pending_approval"
    assert pending["reason"] == "unknown_tool_risk"
    assert calls == []

    ticket = get_ticket(pending["ticket_id"])
    assert ticket is not None
    assert ticket.tool_name == entry["name"]

    approved = approve_ticket(ticket.id)
    assert approved is not None
    resumed = resume_approved_ticket(approved)

    assert resumed.status.value == "applied"
    assert calls == [("transform", {"value": "abc"})]


def test_destructive_tool_requires_approval_even_with_read_only_hint():
    cfg = MCPServerConfig(name="filesystem", command="npx")
    manager = SimpleNamespace(reflected_tool_names=[], call_tool_sync=lambda _tool, _args: "{}")
    tool = SimpleNamespace(
        name="delete_file",
        description="Delete a file",
        inputSchema={"type": "object", "properties": {"path": {"type": "string"}}},
        annotations=SimpleNamespace(readOnlyHint=True),
    )

    entry = build_tool_entries(cfg, manager, [tool])[0]
    pending = json.loads(entry["callable"](path=r"C:\Repo\file.txt"))

    assert pending["status"] == "pending_approval"
    assert pending["reason"] == "destructive_or_mutating_tool"


def test_changed_mcp_schema_invalidates_existing_approval():
    calls: list[tuple[str, dict]] = []
    cfg = MCPServerConfig(name="remote", command="npx")
    manager = SimpleNamespace(
        reflected_tool_names=[],
        call_tool_sync=lambda tool_name, arguments: (
            calls.append((tool_name, arguments)),
            json.dumps({"ok": True}),
        )[1],
    )
    first_tool = SimpleNamespace(
        name="transform",
        description="Transform data",
        inputSchema={"type": "object", "properties": {"value": {"type": "string"}}},
    )
    second_tool = SimpleNamespace(
        name="transform",
        description="Transform data",
        inputSchema={"type": "object", "properties": {"value": {"type": "integer"}}},
    )

    entry = build_tool_entries(cfg, manager, [first_tool])[0]
    pending = json.loads(entry["callable"](value="abc"))
    ticket = get_ticket(pending["ticket_id"])
    assert ticket is not None
    build_tool_entries(cfg, manager, [second_tool])

    approved = approve_ticket(ticket.id)
    assert approved is not None
    resumed = resume_approved_ticket(approved)

    assert resumed.status.value == "failed"
    assert "schema changed" in (resumed.execution_result or "")
    assert calls == []


def test_oversized_mcp_arguments_are_rejected_before_approval():
    cfg = MCPServerConfig(name="remote", command="npx")
    manager = SimpleNamespace(reflected_tool_names=[], call_tool_sync=lambda _tool, _args: "{}")
    tool = SimpleNamespace(
        name="transform",
        description="Transform data",
        inputSchema={"type": "object", "properties": {"value": {"type": "string"}}},
    )

    entry = build_tool_entries(cfg, manager, [tool])[0]
    result = json.loads(entry["callable"](value="x" * (70 * 1024)))

    assert result["status"] == "error"
    assert result["reason_code"] == "mcp_argument_size_exceeded"


def test_server_manager_call_tool_sync_returns_structured_json():
    cfg = MCPServerConfig(name="filesystem", command="npx", call_timeout_ms=1000)
    manager = ServerManager(cfg)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=_run_loop, args=(loop,), daemon=True)
    thread.start()

    try:
        manager._loop = loop
        manager._session = _FakeSession()
        manager.connected = True

        payload = json.loads(manager.call_tool_sync("list_directory", {"path": r"C:\Repo"}))

        assert payload["ok"] is True
        assert payload["tool"] == "list_directory"
        assert payload["server"] == "filesystem"
        assert payload["structured_content"]["arguments"]["path"] == r"C:\Repo"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_register_tools_includes_control_tools_without_enabled_servers():
    registry = ToolRegistry()
    settings = _settings_with_servers([])

    register_tools(registry, settings)

    assert {tool["name"] for tool in registry.get_all_tools()} == {
        "mcp_status",
        "mcp_list_tools",
        "mcp_refresh_tools",
        "mcp_reconnect_server",
    }


def test_register_tools_adds_entries_for_enabled_servers(monkeypatch):
    class FakeManager:
        def __init__(self):
            self.connected = False
            self.state = "stopped"
            self.tool_count = 0
            self.last_error = ""
            self.reflected_tool_names = []
            self.tools = [
                SimpleNamespace(
                    name="list_directory",
                    description="List directory contents",
                    inputSchema={"type": "object", "properties": {"path": {"type": "string"}}},
                )
            ]

        def start(self):
            self.connected = True
            self.state = "connected"
            self.tool_count = len(self.tools)
            return True

        def call_tool_sync(self, tool_name, arguments):
            return json.dumps({"tool": tool_name, "arguments": arguments})

    manager = FakeManager()
    monkeypatch.setattr(mcp_tools_module.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(mcp_tools_module, "ensure_mcp_manager", lambda cfg: manager)

    registry = ToolRegistry()
    register_tools(
        registry,
        _settings_with_servers(
            [
                {
                    "name": "filesystem",
                    "enabled": True,
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", r"C:\Repo"],
                }
            ]
        ),
    )

    tool = registry.get_tool("mcp__filesystem__list_directory")
    assert tool is not None
    assert json.loads(tool["callable"](path=r"C:\Repo"))["tool"] == "list_directory"


def test_control_tools_remain_when_server_start_fails(monkeypatch):
    class FailingManager:
        connected = False
        state = "failed"
        tool_count = 0
        last_error = "boom"
        reflected_tool_names: list[str] = []
        tools: list[object] = []

        def start(self):
            return False

    monkeypatch.setattr(mcp_tools_module.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(mcp_tools_module, "ensure_mcp_manager", lambda cfg: FailingManager())

    registry = ToolRegistry()
    register_tools(
        registry,
        _settings_with_servers([{"name": "filesystem", "enabled": True, "command": "npx"}]),
    )

    assert {tool["name"] for tool in registry.get_all_tools()} == {
        "mcp_status",
        "mcp_list_tools",
        "mcp_refresh_tools",
        "mcp_reconnect_server",
    }


def test_refresh_control_requires_approval_and_resume_updates_registry(monkeypatch):
    class RefreshingManager:
        def __init__(self):
            self.connected = True
            self.state = "connected"
            self.tool_count = 0
            self.last_error = ""
            self.reflected_tool_names = []
            self.tools: list[object] = []
            self.refresh_count = 0

        def refresh_tools(self):
            self.refresh_count += 1
            tool_name = "list_alpha" if self.refresh_count == 1 else "list_beta"
            self.tools = [
                SimpleNamespace(
                    name=tool_name,
                    description="List data",
                    inputSchema={"type": "object", "properties": {}},
                )
            ]
            self.tool_count = len(self.tools)
            return True

        def call_tool_sync(self, tool_name, arguments):
            return json.dumps({"tool": tool_name, "arguments": arguments})

    manager = RefreshingManager()
    monkeypatch.setattr(mcp_tools_module.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(mcp_tools_module, "ensure_mcp_manager", lambda cfg: manager)

    registry = ToolRegistry()
    register_tools(registry, _settings_with_servers([{"name": "filesystem", "enabled": True, "command": "npx"}]))

    assert registry.get_tool("mcp__filesystem__list_alpha") is not None

    refresh_tool = registry.get_tool("mcp_refresh_tools")
    assert refresh_tool is not None
    pending = json.loads(refresh_tool["callable"](server_name="filesystem"))
    assert pending["status"] == "pending_approval"

    ticket = get_ticket(pending["ticket_id"])
    assert ticket is not None
    approved = approve_ticket(ticket.id)
    assert approved is not None
    resumed = resume_approved_ticket(approved)
    result = json.loads(resumed.execution_result)

    assert result["ok"] is True
    assert registry.get_tool("mcp__filesystem__list_alpha") is None
    assert registry.get_tool("mcp__filesystem__list_beta") is not None


def test_reconnect_control_requires_approval(monkeypatch):
    monkeypatch.setattr(mcp_tools_module.importlib.util, "find_spec", lambda name: None)
    registry = ToolRegistry()
    register_tools(registry, _settings_with_servers([{"name": "filesystem", "enabled": True, "command": "npx"}]))

    reconnect_tool = registry.get_tool("mcp_reconnect_server")
    assert reconnect_tool is not None
    pending = json.loads(reconnect_tool["callable"](server_name="filesystem"))

    assert pending["status"] == "pending_approval"
    ticket = get_ticket(pending["ticket_id"])
    assert ticket is not None
    assert ticket.action_type == "mcp_control"
    assert ticket.tool_name == "mcp_reconnect_server"


def test_failed_refresh_prunes_reflected_metadata(monkeypatch):
    cfg = MCPServerConfig(name="filesystem", command="npx")
    registry = ToolRegistry(
        build_tool_entries(
            cfg,
            SimpleNamespace(reflected_tool_names=[], call_tool_sync=lambda _tool, _args: "{}"),
            [SimpleNamespace(name="list_directory", description="List", inputSchema={"type": "object", "properties": {}})],
        )
    )
    assert registry.get_tool("mcp__filesystem__list_directory") is not None
    assert get_reflected_tool_map()

    monkeypatch.setattr(mcp_tools_module.importlib.util, "find_spec", lambda name: None)

    result = mcp_tools_module._refresh_server_tools(registry, cfg)

    assert result["ok"] is False
    assert registry.get_tool("mcp__filesystem__list_directory") is None
    assert get_reflected_tool_map() == {}


def test_call_timeout_marks_manager_unhealthy():
    cfg = MCPServerConfig(name="slow", command="npx", call_timeout_ms=10)
    manager = ServerManager(cfg)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=_run_loop, args=(loop,), daemon=True)
    thread.start()

    try:
        manager._loop = loop
        manager._session = _SlowSession()
        manager.connected = True

        payload = json.loads(manager.call_tool_sync("read_slow", {}))

        assert payload["ok"] is False
        assert payload["error"]["code"] == "mcp_timeout"
        assert manager.state == "unhealthy"
        assert manager.failed_call_count == 1
        assert "timed out" in manager.unhealthy_reason
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_liveness_probe_marks_dead_server_unhealthy_and_publishes_event(monkeypatch):
    from app.skills.mcp_bridge import connection as connection_module

    class DeadSession:
        async def list_tools(self):
            raise RuntimeError("child exited")

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        connection_module,
        "publish_ui_event",
        lambda event, data: events.append((event, data)),
    )
    manager = ServerManager(
        MCPServerConfig(name="dead", transport="streamable_http", url="http://127.0.0.1:1")
    )
    manager._stop = asyncio.Event()
    manager.connected = True
    manager.state = "connected"
    manager.liveness_interval_seconds = 0.01
    manager.liveness_failure_threshold = 3

    asyncio.run(manager._probe_liveness(DeadSession()))

    assert manager.connected is False
    assert manager.state == "unhealthy"
    assert manager.unhealthy_reason == "MCP liveness check failed: child exited"
    assert manager._stop.is_set()
    assert any(
        event == "mcp.changed" and data["state"] == "unhealthy"
        for event, data in events
    )


def test_stdio_liveness_uses_process_check_without_list_tools(monkeypatch):
    class ExplodingSession:
        async def list_tools(self):
            raise AssertionError("stdio liveness must not issue a full tool listing")

    manager = ServerManager(MCPServerConfig(name="stdio-dead", command="node"))
    manager._stop = asyncio.Event()
    manager.connected = True
    manager.state = "connected"
    manager.liveness_interval_seconds = 0.01
    manager.liveness_failure_threshold = 1
    monkeypatch.setattr(manager, "_stdio_process_alive", lambda: False)

    asyncio.run(manager._probe_liveness(ExplodingSession()))

    assert manager.state == "unhealthy"
    assert manager.unhealthy_reason == "MCP stdio child process is no longer running"
    assert manager._stop.is_set()


def test_stop_current_runtime_preserves_stuck_thread_for_diagnostics():
    class StuckThread:
        joined_with: float | None = None

        def is_alive(self):
            return True

        def join(self, timeout=None):
            self.joined_with = timeout

    cfg = MCPServerConfig(name="stuck", command="npx")
    manager = ServerManager(cfg)
    stuck = StuckThread()
    manager._thread = stuck
    manager.connected = True
    manager.state = "connected"

    stopped = manager._stop_current_runtime_locked(join_timeout=0.01)

    assert stopped is False
    assert manager._thread is stuck
    assert stuck.joined_with == 0.01
    assert manager.state == "unhealthy"
    assert manager.startup_phase == "stop_timeout"
    assert "did not stop" in manager.unhealthy_reason


def test_clear_reflected_tools_for_server_only_removes_matching_server():
    build_tool_entries(
        MCPServerConfig(name="one", command="npx"),
        SimpleNamespace(reflected_tool_names=[], call_tool_sync=lambda _tool, _args: "{}"),
        [SimpleNamespace(name="list_one", description="List", inputSchema={"type": "object", "properties": {}})],
    )
    build_tool_entries(
        MCPServerConfig(name="two", command="npx"),
        SimpleNamespace(reflected_tool_names=[], call_tool_sync=lambda _tool, _args: "{}"),
        [SimpleNamespace(name="list_two", description="List", inputSchema={"type": "object", "properties": {}})],
    )

    clear_reflected_tools_for_server("one")

    mapping = get_reflected_tool_map()
    assert "mcp__one__list_one" not in mapping
    assert mapping["mcp__two__list_two"]["server_name"] == "two"
