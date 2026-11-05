import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agent.approval_broker import approve_ticket
from app.agent.execution_resume import resume_approved_ticket
from app.agent.settings_store import AgentSettings, MCPServerConfig
from app.skills.mcp_bridge.connection import ServerManager
from app.skills.mcp_bridge.registry import build_tool_entries, clear_all_reflected_tools, _schema_hash, _server_identity


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    from app.agent import controller_policy
    settings = AgentSettings()
    monkeypatch.setattr(controller_policy, "_load_runtime_settings", lambda: settings)
    clear_all_reflected_tools()
    yield settings
    clear_all_reflected_tools()


def test_unknown_mcp_review_is_exact_and_server_replacement_invalidates_it(settings):
    calls = []
    cfg = MCPServerConfig(name="remote", command="fixture")
    manager = SimpleNamespace(cfg=cfg, call_tool_sync=lambda name, args: calls.append((name, args)) or '{"ok":true}')
    tool = SimpleNamespace(name="get_data", description="A harmless read", inputSchema={"type": "object"},
                           annotations={"readOnlyHint": True})
    entry = build_tool_entries(cfg, manager, [tool])[0]
    pending = json.loads(entry["callable"](recipient="first"))
    assert pending["status"] == "pending_approval"
    assert pending["human_only"] is True
    approved = approve_ticket(pending["ticket_id"])
    assert json.loads(resume_approved_ticket(approved).execution_result)["ok"] is True
    assert calls == [("get_data", {"recipient": "first"})]
    pending = json.loads(entry["callable"](recipient="second"))
    replacement = SimpleNamespace(cfg=cfg, call_tool_sync=lambda *_: pytest.fail("replacement executed"))
    build_tool_entries(cfg, replacement, [tool])
    approved = approve_ticket(pending["ticket_id"])
    assert resume_approved_ticket(approved).status.value == "failed"
    assert len(calls) == 1


def test_mcp_schema_and_destination_validated_before_dispatch():
    cfg = MCPServerConfig(name="remote", command="fixture", trusted_tools=["send"],
                          allowed_destinations=["https://approved.example"])
    manager = SimpleNamespace(cfg=cfg, call_tool_sync=lambda *_: pytest.fail("blocked tool executed"))
    tool = SimpleNamespace(name="send", inputSchema={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]})
    entry = build_tool_entries(cfg, manager, [tool])[0]
    assert json.loads(entry["callable"](url=123))["reason_code"] == "mcp_arguments_schema_invalid"
    assert json.loads(entry["callable"](url="https://unapproved.example"))["reason_code"] == "mcp_destination_not_allowed"
    assert json.loads(entry["callable"](url="https://approved.example", __monaw_authorization={}))['reason_code'] == "mcp_reserved_argument"


def test_same_count_remote_schema_replacement_rejected_at_final_transport_boundary():
    cfg = MCPServerConfig(name="remote", command="fixture")
    manager = ServerManager(cfg)
    original = {"type": "object", "properties": {"recipient": {"type": "string"}}}
    class Session:
        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name="send", inputSchema={"type": "object"})])
        async def call_tool(self, *_a, **_kw):
            pytest.fail("schema replacement dispatched")
    manager._session = Session()
    with pytest.raises(ValueError, match="mcp_schema_changed"):
        asyncio.run(manager._call_tool("send", {}, expected_scope=(_schema_hash(original), _server_identity(cfg))))


@pytest.mark.parametrize("change", [None, "navigation", "target", "destination"])
def test_real_browser_adapter_approval_replay_checks_live_context(settings, change):
    from app.agent.tests.test_browser_extension_tools import FakeChrome
    from app.skills.browser_use.extension.toolset import BrowserTools, _TARGET_FACTS
    from app.skills.browser_use.extension.session import ToolError

    class Chrome(FakeChrome):
        destination = "https://example.com/submit"
        replacement = False
        async def request(self, method, params=None, **kwargs):
            if method == "cdp" and params["method"] == "Runtime.callFunctionOn" and params["params"].get("functionDeclaration") == _TARGET_FACTS:
                return {"result": {"value": {"tag": "button", "type": "submit", "attached": True,
                    "form_action": self.destination, "form_method": "post", "frame_url": self.tabs[0]["url"],
                    "replacement": self.replacement}}}
            return await super().request(method, params, **kwargs)

    chrome = Chrome()
    tools = BrowserTools(chrome, require_authorization=True)
    class Manager:
        cfg = MCPServerConfig(name="chrome-devtools", command="controlled-extension-fixture")
        def call_tool_sync(self, name, args):
            try:
                output = asyncio.run(tools.call(name, args))
                return json.dumps({"status": "ok", "structured_content": {
                    **output.structured_content, **({"action_outcome": output.action_outcome} if output.action_outcome else {})}})
            except ToolError as exc:
                return json.dumps({"status": "blocked", "error": str(exc)})
    manager = Manager()
    snapshot = asyncio.run(tools._tool_take_snapshot({}))
    assert 'uid=1_3 button' in snapshot.lines[0]
    entry = build_tool_entries(manager.cfg, manager, [SimpleNamespace(name="click", inputSchema={"type": "object"})])[0]
    pending = json.loads(entry["callable"](uid="1_3", includeSnapshot=False))
    assert pending["status"] == "pending_approval", pending
    assert not any(name == "Input.dispatchMouseEvent" for name, _ in chrome.calls)
    if change == "navigation":
        chrome.loader = "replaced-document"
    elif change == "target":
        chrome.replacement = True
    elif change == "destination":
        chrome.destination = "https://other.example/submit"
    approved = approve_ticket(pending["ticket_id"])
    result = resume_approved_ticket(approved)
    dispatched = [args for name, args in chrome.calls if name == "Input.dispatchMouseEvent"]
    if change is None:
        assert result.status.value == "applied", result.execution_result
        assert json.loads(result.execution_result)["structured_content"]["action_outcome"]["action_executed"]
        assert len(dispatched) == 3
        resume_approved_ticket(approved)
        assert len([1 for name, _ in chrome.calls if name == "Input.dispatchMouseEvent"]) == 3
    else:
        assert result.status.value == "failed", result.execution_result
        assert not dispatched
