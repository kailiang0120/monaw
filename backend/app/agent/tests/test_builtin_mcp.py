import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.agent import controller_policy
from app.agent.approval_broker import get_ticket
from app.agent.harness.tool_policy import ToolPolicy
from app.agent.harness.tool_protocol import ToolCallRequest
from app.agent.run_context import reset_current_interactive, set_current_interactive
from app.agent.settings_store import AgentSettings, MCPServerConfig, _normalize_loaded_payload, build_runtime_namespace
from app.agent.settings_store import merge_agent_settings
from app.agent.tool_registry import ToolRegistry
from app.main import app
from app.skills.mcp_bridge import builtin, connection
from app.skills.mcp_bridge import tools as bridge_tools
from app.skills.mcp_bridge.registry import build_tool_entries, clear_all_reflected_tools


def _runtime(settings_data: AgentSettings):
    base = type("Base", (), {
        "openai_api_key": "", "google_api_key": "", "tavily_api_key": "",
    })()
    return build_runtime_namespace(base, settings_data)


def test_chrome_devtools_ships_as_a_builtin_on_the_users_chrome(monkeypatch):
    monkeypatch.delenv(builtin.DISABLE_ENV, raising=False)

    [server] = builtin.builtin_mcp_servers()

    assert server.name == builtin.CHROME_DEVTOOLS_SERVER
    assert server.command == "npx"
    assert "--autoConnect" in server.args
    assert builtin.BUILTIN_SERVER_SKILLS[server.name] == "browser-use"

    monkeypatch.setenv(builtin.DISABLE_ENV, "1")
    assert builtin.builtin_mcp_servers() == []


def test_builtin_servers_run_even_when_the_user_turns_mcp_off(monkeypatch):
    monkeypatch.delenv(builtin.DISABLE_ENV, raising=False)
    settings_data = AgentSettings()
    settings_data.mcp.servers = [MCPServerConfig(name="my-files", enabled=True)]

    enabled = [cfg.name for cfg in bridge_tools._iter_enabled_servers(_runtime(settings_data))]
    assert enabled == ["my-files", "chrome-devtools"]
    assert [cfg.name for cfg in connection._iter_enabled_configs(settings_data)] == ["my-files", "chrome-devtools"]

    settings_data.mcp.enabled = False
    assert [cfg.name for cfg in bridge_tools._iter_enabled_servers(_runtime(settings_data))] == ["chrome-devtools"]
    assert [cfg.name for cfg in connection._iter_enabled_configs(settings_data)] == ["chrome-devtools"]


def test_a_user_server_cannot_shadow_the_builtin_name(monkeypatch):
    monkeypatch.delenv(builtin.DISABLE_ENV, raising=False)
    settings_data = AgentSettings()
    settings_data.mcp.servers = [MCPServerConfig(name="chrome-devtools", enabled=True, command="evil")]

    servers = bridge_tools._iter_enabled_servers(_runtime(settings_data))

    assert [cfg.command for cfg in servers] == ["npx"]


def test_saved_chrome_devtools_entries_are_dropped_on_load():
    payload = _normalize_loaded_payload({
        "mcp": {
            "enabled": True,
            "servers": [
                {"name": "Chrome-dev-tools", "command": "npx", "args": ["-y", "chrome-devtools-mcp@latest", "--autoConnect"]},
                {"name": "my-files", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"]},
            ],
        },
    })

    assert [server["name"] for server in payload["mcp"]["servers"]] == ["my-files"]
    assert AgentSettings.model_validate(payload).mcp.servers[0].name == "my-files"


def test_mcp_diagnostics_list_hides_builtin_servers(monkeypatch):
    settings_data = AgentSettings()
    settings_data.mcp.servers = [MCPServerConfig(name="my-files", enabled=True)]
    runtime_entry = {"enabled": True, "transport": "stdio", "connected": True, "state": "connected", "tool_count": 1}
    monkeypatch.setattr(
        "app.api.routes.diagnostics.get_mcp_runtime_diagnostics",
        lambda: [{**runtime_entry, "name": "my-files"}, {**runtime_entry, "name": "chrome-devtools"}],
    )
    monkeypatch.setattr("app.api.routes.diagnostics.load_agent_settings", lambda _settings: settings_data)

    response = TestClient(app).get("/api/diagnostics/mcp")

    assert response.status_code == 200
    assert [item["name"] for item in response.json()] == ["my-files"]


# --- Pinned server, explicit permissions, repeat guard, URL guard ---


@pytest.fixture
def runtime_settings(monkeypatch):
    settings_data = AgentSettings()
    monkeypatch.setattr(controller_policy, "_load_runtime_settings", lambda: settings_data)
    clear_all_reflected_tools()
    yield settings_data
    clear_all_reflected_tools()


def _devtools_entries(*tool_names, calls=None):
    calls = calls if calls is not None else []
    cfg = MCPServerConfig(name=builtin.CHROME_DEVTOOLS_SERVER, command="npx")
    manager = SimpleNamespace(
        reflected_tool_names=[],
        call_tool_sync=lambda tool, args: calls.append((tool, args)) or json.dumps({"ok": True, "status": "ok"}),
    )
    tools = [SimpleNamespace(name=name, description="Chrome browser tool", inputSchema={"type": "object"})
             for name in tool_names]
    return {entry["mcp_bridge"]["original_tool_name"]: entry for entry in build_tool_entries(cfg, manager, tools)}


def test_chrome_devtools_server_is_pinned_and_hardened(monkeypatch):
    monkeypatch.delenv(builtin.DISABLE_ENV, raising=False)

    [server] = builtin.builtin_mcp_servers()

    assert server.args == [
        "-y", "chrome-devtools-mcp@1.11.0", "--autoConnect", "--no-usage-statistics", "--no-performance-crux",
        "--no-category-memory", "--no-category-performance", "--no-category-extensions",
        "--no-file-navigations", "--redact-network-headers",
    ]
    assert server.env == {
        "CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS": "1",
        "CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS": "1",
    }


def test_devtools_reads_never_prompt_and_are_observations(runtime_settings):
    calls = []
    reads = ["list_pages", "select_page", "take_snapshot", "take_screenshot", "wait_for", "list_console_messages",
             "get_console_message", "list_network_requests", "get_network_request", "get_css_styles"]
    entries = _devtools_entries(*reads, calls=calls)

    for name in reads:
        assert json.loads(entries[name]["callable"]())["ok"] is True
        assert entries[name]["metadata"] == {"observation": True, "mutates_state": False, "risk_level": "low"}
        assert entries[name]["mcp_bridge"]["approval"]["action"] == "read"
    assert [tool for tool, _ in calls] == reads


def test_devtools_interactions_run_without_prompt_but_not_unattended(runtime_settings):
    calls = []
    expected_actions = {"click": "click", "hover": "click", "drag": "click", "handle_dialog": "click",
                        "fill": "type", "fill_form": "type", "type_text": "type", "press_key": "type",
                        "navigate_page": "launch_app", "new_page": "launch_app", "close_page": "launch_app"}
    entries = _devtools_entries(*expected_actions, calls=calls)

    for name, action in expected_actions.items():
        approval = entries[name]["mcp_bridge"]["approval"]
        assert (approval["action"], approval["risk"]) == (action, "medium")
        args = {"url": "https://example.com"} if name in {"navigate_page", "new_page"} else {"uid": "1_2"}
        assert json.loads(entries[name]["callable"](**args))["ok"] is True
        assert entries[name]["metadata"]["mutates_state"] is True
    assert len(calls) == len(expected_actions)

    token = set_current_interactive(False)
    try:
        assert json.loads(entries["click"]["callable"](uid="1_2"))["reason_code"] == "non_interactive_restricted"
    finally:
        reset_current_interactive(token)


def test_devtools_script_and_file_tools_prompt_with_table_risk(runtime_settings):
    calls = []
    entries = _devtools_entries("evaluate_script", "upload_file", "lighthouse_audit", calls=calls)

    pending = json.loads(entries["evaluate_script"]["callable"](function="() => 1"))
    assert pending["status"] == "pending_approval"
    assert get_ticket(pending["ticket_id"]).risk_level == "high"

    upload = json.loads(entries["upload_file"]["callable"](uid="1_2", filePath="C:/x.txt"))
    assert upload["status"] == "pending_approval"
    assert get_ticket(upload["ticket_id"]).risk_level == "high"

    audit = json.loads(entries["lighthouse_audit"]["callable"]())
    assert audit["status"] == "pending_approval"
    assert get_ticket(audit["ticket_id"]).risk_level == "medium"

    # Full access skips the trust prompts, except upload_file which always asks.
    runtime_settings.permissions = merge_agent_settings(
        runtime_settings, {"permissions": {"mode": "full_access"}}).permissions
    assert json.loads(entries["evaluate_script"]["callable"](function="() => 1"))["ok"] is True
    assert json.loads(entries["upload_file"]["callable"](uid="1_2", filePath="C:/x.txt"))["status"] == "pending_approval"
    assert [tool for tool, _ in calls] == ["evaluate_script"]


def test_devtools_tools_outside_the_table_use_keyword_classification(runtime_settings):
    entry = _devtools_entries("install_extension")["install_extension"]

    assert "action" not in entry["mcp_bridge"]["approval"]
    assert entry["mcp_bridge"]["approval"]["reason"] == "open_world_tool"


def test_repeat_guard_allows_a_third_identical_take_snapshot(runtime_settings):
    entries = _devtools_entries("take_snapshot", "evaluate_script")
    registry = ToolRegistry(list(entries.values()))
    policy = ToolPolicy(max_identical_calls=2)

    def decide(name: str, index: int):
        return policy.decide(request=ToolCallRequest(call_id=str(index), name=name, arguments={}), registry=registry)

    snapshots = [decide(entries["take_snapshot"]["name"], index) for index in range(3)]
    assert all(decision.allowed for decision in snapshots)
    assert snapshots[-1].risk == "low"

    scripts = [decide(entries["evaluate_script"]["name"], index) for index in range(3)]
    assert scripts[-1].metadata["code"] == "repeated_tool_call_blocked"


def test_readonly_hint_marks_any_mcp_tool_as_observation(runtime_settings):
    manager = SimpleNamespace(reflected_tool_names=[], call_tool_sync=lambda *_: "{}")
    tools = [
        SimpleNamespace(name="list_notes", description="List notes", annotations={"readOnlyHint": True}),
        SimpleNamespace(name="delete_note", description="Delete a note",
                        annotations={"readOnlyHint": True, "destructiveHint": True}),
    ]
    entries = build_tool_entries(MCPServerConfig(name="notes", command="npx"), manager, tools)

    assert entries[0]["metadata"] == {"observation": True, "mutates_state": False, "risk_level": "low"}
    assert entries[1]["metadata"] == {}


@pytest.mark.parametrize("url,reason_code", [
    ("file:///C:/Users/someone/secret.txt", "url_scheme_not_allowed"),
    ("javascript:alert(1)", "url_scheme_not_allowed"),
    ("data:text/html,<h1>x</h1>", "url_scheme_not_allowed"),
    ("chrome://settings", "url_scheme_not_allowed"),
    ("https://evil.example.net/", "domain_not_allowed"),
])
def test_devtools_navigation_is_checked_against_the_browser_url_policy(runtime_settings, url, reason_code):
    runtime_settings.browser.allowed_domains = ["example.com"]
    calls = []
    entries = _devtools_entries("navigate_page", "new_page", calls=calls)

    for name in ("navigate_page", "new_page"):
        result = json.loads(entries[name]["callable"](url=url))
        assert result["status"] == "blocked"
        assert result["reason_code"] == reason_code
    assert calls == []


def test_devtools_navigation_passes_allowed_urls_and_history_moves(runtime_settings):
    runtime_settings.browser.allowed_domains = ["example.com"]
    calls = []
    navigate = _devtools_entries("navigate_page", calls=calls)["navigate_page"]["callable"]

    assert json.loads(navigate(url="docs.example.com/start"))["ok"] is True
    assert json.loads(navigate(type="back"))["ok"] is True
    blocked = json.loads(navigate(url="https://example.com", initScript="alert(1)"))

    assert blocked["reason_code"] == "init_script_not_allowed"
    assert calls == [("navigate_page", {"url": "https://docs.example.com/start"}), ("navigate_page", {"type": "back"})]


def test_devtools_reads_stay_available_to_unattended_runs(runtime_settings):
    entries = _devtools_entries("list_pages", "take_snapshot")
    token = set_current_interactive(False)
    try:
        assert json.loads(entries["list_pages"]["callable"]())["ok"] is True
        assert json.loads(entries["take_snapshot"]["callable"]())["ok"] is True
    finally:
        reset_current_interactive(token)
