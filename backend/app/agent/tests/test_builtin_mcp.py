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


def test_browser_server_ships_as_a_builtin_served_by_the_extension(monkeypatch):
    import sys

    monkeypatch.delenv(builtin.DISABLE_ENV, raising=False)
    settings_data = AgentSettings()
    settings_data.browser.extension_port = 8500

    [server] = builtin.builtin_mcp_servers(settings_data)

    assert server.name == builtin.CHROME_DEVTOOLS_SERVER
    assert server.command == sys.executable
    assert server.args == ["-m", "app.skills.browser_use.extension"]
    assert server.env == {"MONAW_BROWSER_EXTENSION_PORT": "8500"}
    assert builtin.BUILTIN_SERVER_SKILLS[server.name] == "browser-use"
    # Chrome remote debugging (chrome-devtools-mcp --autoConnect, npx) is gone.
    assert "npx" not in server.command and not any("chrome-devtools-mcp" in arg for arg in server.args)

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

    assert [cfg.args for cfg in servers] == [["-m", "app.skills.browser_use.extension"]]


def test_saved_remote_debugging_entries_are_dropped_on_load():
    payload = _normalize_loaded_payload({
        "mcp": {
            "enabled": True,
            "servers": [
                {"name": "Chrome-dev-tools", "command": "npx", "args": ["-y", "chrome-devtools-mcp@latest", "--autoConnect"]},
                {"name": "my-files", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"]},
                {"name": "devtools-headless", "command": "npx", "args": ["-y", "chrome-devtools-mcp@latest", "--headless"]},
            ],
        },
    })

    # Only old --autoConnect copies are dropped; other servers the user added stay.
    assert [server["name"] for server in payload["mcp"]["servers"]] == ["my-files", "devtools-headless"]
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
    def controlled_context(_manager, name, arguments, upload_scope=None):
        policy = builtin.builtin_tool_policy(builtin.CHROME_DEVTOOLS_SERVER, name)
        action = {"exec":"execute_code", "launch_app":"navigate", "mutate":"upload"}.get(policy.action.value, policy.action.value)
        return {"context_token":"controlled-test-context", "context_version":"document:1", "url":"https://example.com/",
                "target":{"page_id":arguments.get("pageId", 1)}, "destination":arguments.get("url", "https://example.com/"),
                "action_kind":action, "side_effect":"unknown" if action == "execute_code" else "none" if policy.read_only else "local_mutation",
                "runtime_facts":{"target_verified":True,"context_verified":True}, "data_scope":upload_scope or {}}
    monkeypatch.setattr("app.skills.mcp_bridge.registry._browser_context", controlled_context)
    clear_all_reflected_tools()
    yield settings_data
    clear_all_reflected_tools()


def _devtools_entries(*tool_names, calls=None):
    calls = calls if calls is not None else []
    cfg = MCPServerConfig(name=builtin.CHROME_DEVTOOLS_SERVER, command="npx")
    manager = SimpleNamespace(
        reflected_tool_names=[],
        call_tool_sync=lambda tool, args: calls.append((tool, {key:value for key,value in args.items() if key != "__monaw_authorization"})) or json.dumps({"ok": True, "status": "ok"}),
    )
    tools = [SimpleNamespace(name=name, description="Chrome browser tool", inputSchema={"type": "object"})
             for name in tool_names]
    return {entry["mcp_bridge"]["original_tool_name"]: entry for entry in build_tool_entries(cfg, manager, tools)}


def test_policy_table_covers_exactly_the_extension_tools():
    from app.skills.browser_use.extension.tools_schema import TOOL_NAMES

    assert set(builtin.BUILTIN_TOOL_POLICIES[builtin.CHROME_DEVTOOLS_SERVER]) == set(TOOL_NAMES)


def test_devtools_reads_never_prompt_and_are_observations(runtime_settings):
    calls = []
    reads = ["list_pages", "select_page", "take_snapshot", "inspect_element", "take_screenshot", "wait_for", "list_console_messages",
             "get_console_message"]
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


def test_devtools_script_and_file_tools_prompt_with_table_risk(runtime_settings, tmp_path):
    calls = []
    entries = _devtools_entries("evaluate_script", "upload_file", calls=calls)

    pending = json.loads(entries["evaluate_script"]["callable"](pageId=1, function="() => 1"))
    assert pending["status"] == "pending_approval"
    assert get_ticket(pending["ticket_id"]).risk_level == "high"

    report = tmp_path / "report.txt"
    report.write_text("report")
    runtime_settings.browser.upload_workspace_roots = [str(tmp_path)]
    runtime_settings.permissions.blocked_roots = []
    runtime_settings.permissions = merge_agent_settings(runtime_settings, {"permissions": {"mode": "full_access"}}).permissions
    upload = json.loads(entries["upload_file"]["callable"](uid="1_2", filePaths=[str(report)]))
    assert upload["status"] == "pending_approval"
    assert get_ticket(upload["ticket_id"]).risk_level == "high"

    # Full access skips the trust prompts, except upload_file which always asks.
    runtime_settings.permissions = merge_agent_settings(
        runtime_settings, {"permissions": {"mode": "full_access"}}).permissions
    assert json.loads(entries["evaluate_script"]["callable"](pageId=1, function="() => 1"))["ok"] is True
    assert json.loads(entries["upload_file"]["callable"](uid="1_2", filePaths=[str(report)]))["status"] == "pending_approval"
    assert [tool for tool, _ in calls] == ["evaluate_script"]


@pytest.mark.parametrize(("mode", "prompts"), [("default", True), ("custom", True), ("auto_review", True), ("full_access", False)])
def test_script_execution_keeps_its_capability_and_exact_target_in_each_mode(runtime_settings, mode, prompts):
    runtime_settings.permissions = merge_agent_settings(runtime_settings, {
        "permissions": {"mode": mode, **({"dangerous_actions_require_confirm": True} if mode == "custom" else {})},
    }).permissions
    calls = []
    cfg = MCPServerConfig(name=builtin.CHROME_DEVTOOLS_SERVER, command="fixture",
                          trusted_tools=["evaluate_script"], tool_risk_overrides={"evaluate_script": "low"})
    manager = SimpleNamespace(reflected_tool_names=[], call_tool_sync=lambda name, args: calls.append({key:value for key,value in args.items() if key != "__monaw_authorization"}) or '{"status":"ok"}')
    [entry] = build_tool_entries(cfg, manager, [SimpleNamespace(
        name="evaluate_script", description="Read-only DOM inspection", inputSchema={"type": "object"},
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )])
    assert entry["metadata"]["capability"] == "code_execution"
    assert entry["metadata"]["mutates_state"] is True and entry["metadata"]["observation"] is False
    assert entry["metadata"]["risk_level"] == "high"
    assert json.loads(entry["callable"](function="() => document.title"))["reason_code"] == "script_target_required"
    arguments = {"pageId": 7, "function": "() => document.title"}
    result = json.loads(entry["callable"](**arguments))
    if prompts:
        ticket = get_ticket(result["ticket_id"])
        assert result["status"] == "pending_approval"
        assert ticket.payload["args"] == arguments
        assert ticket.action_descriptor["action_kind"] == "execute_code"
        assert calls == []
    else:
        assert result["status"] == "ok" and calls == [arguments]


def test_devtools_tools_outside_the_table_are_blocked(runtime_settings):
    entry = _devtools_entries("install_extension")["install_extension"]

    assert entry["mcp_bridge"]["approval"]["action"] == "exec"
    assert json.loads(entry["callable"]())["reason_code"] == "unclassified_builtin_tool"


def test_repeat_guard_allows_a_third_identical_take_snapshot(runtime_settings):
    entries = _devtools_entries("take_snapshot", "evaluate_script")
    registry = ToolRegistry(list(entries.values()))
    policy = ToolPolicy(max_identical_calls=2)

    def decide(name: str, index: int):
        return policy.decide(request=ToolCallRequest(call_id=str(index), name=name, arguments={}), registry=registry)

    snapshots = [decide(entries["take_snapshot"]["name"], index) for index in range(3)]
    assert all(decision.allowed for decision in snapshots)
    assert snapshots[-1].risk == "high"  # MCP display risk cannot be lowered by provider metadata.

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


@pytest.mark.parametrize("tool,arguments", [
    ("select_page", {"pageId": 1}),
    ("take_snapshot", {}),
    ("take_screenshot", {}),
    ("click", {"uid": "link-to-another-origin"}),
    ("navigate_page", {"type": "back"}),
    ("navigate_page", {"type": "forward"}),
    ("navigate_page", {"type": "reload"}),
])
def test_navigation_target_policy_does_not_inspect_tabs_or_implicit_destinations(runtime_settings, tool, arguments):
    from app.skills.mcp_bridge.registry import _url_guard

    runtime_settings.browser.allowed_domains = ["example.com"]
    normalized, error = _url_guard(builtin.CHROME_DEVTOOLS_SERVER, tool, arguments)
    assert error is None
    assert normalized == arguments


def test_chrome_response_destinations_are_not_a_navigation_target_boundary(runtime_settings):
    runtime_settings.browser.allowed_domains = ["example.com"]
    # Controlled adapter: Chrome reports a redirect or popup on another origin.
    # The guard checks the submitted URL, not the browser's resulting targets.
    response = {"status": "ok", "final_url": "https://outside.example.net/",
                "popup_url": "https://another.example.net/"}
    cfg = MCPServerConfig(name=builtin.CHROME_DEVTOOLS_SERVER, command="npx")
    manager = SimpleNamespace(reflected_tool_names=[], call_tool_sync=lambda *_: json.dumps(response))
    [entry] = build_tool_entries(cfg, manager, [SimpleNamespace(
        name="navigate_page", description="Navigate", inputSchema={"type": "object"})])
    assert json.loads(entry["callable"](url="https://example.com/start")) == response


def test_devtools_reads_stay_available_to_unattended_runs(runtime_settings):
    runtime_settings.browser.allowed_domains = ["example.com"]
    entries = _devtools_entries("list_pages", "take_snapshot")
    token = set_current_interactive(False)
    try:
        assert json.loads(entries["list_pages"]["callable"]())["ok"] is True
        assert json.loads(entries["take_snapshot"]["callable"]())["ok"] is True
    finally:
        reset_current_interactive(token)


def test_changing_the_extension_port_restarts_mcp():
    from app.api.routes.settings import _browser_connection_changed

    current = AgentSettings()
    assert _browser_connection_changed(current, merge_agent_settings(current, {"browser": {"extension_port": 8500}}))
    assert not _browser_connection_changed(current, merge_agent_settings(current, {"browser": {"allowed_domains": ["a.com"]}}))
