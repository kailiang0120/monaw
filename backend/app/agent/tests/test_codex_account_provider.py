"""OpenAI account provider keeps auth and execution separate from API keys."""

import asyncio
import json
from pathlib import Path

from app.agent import codex_app_server
from app.agent.codex_account import CodexAccountService, _rate_limit_windows
from app.agent.codex_app_server import default_server_response, write_model_catalog
from app.agent.harness.tool_protocol import ToolCallResult
from app.agent.llm_client import LLMClient, build_tool_result_message
from app.agent.model_catalog import model_options_payload
from app.agent.settings_store import LLMSettings

TOOLS = [{"name": "search", "description": "Search", "parameters": {"type": "object"}}]


class FakeServer:
    """Scripted stand-in for the Codex app-server JSON-RPC connection."""

    def __init__(self, turn_script=None, resume_script=None):
        self.requests: list[tuple[str, dict | None]] = []
        self.responses: list[tuple[object, dict | None, dict | None]] = []
        self.queues: dict[str, asyncio.Queue] = {}
        self.turn_script = turn_script or []
        self.resume_script = resume_script or []
        self.threads = 0
        self.running = True
        self.account = {"type": "chatgpt", "planType": "plus"}

    async def ensure_started(self, *, require_catalog=False):
        return None

    def subscribe(self, thread_id):
        return self.queues.setdefault(thread_id, asyncio.Queue())

    def unsubscribe(self, thread_id):
        self.queues.pop(thread_id, None)

    def _push(self, thread_id, script):
        for message in script:
            message = json.loads(json.dumps(message))
            message.setdefault("params", {})["threadId"] = thread_id
            self.queues[thread_id].put_nowait(message)

    async def request(self, method, params=None):
        self.requests.append((method, params))
        if method == "account/read":
            return {"account": self.account}
        if method == "account/rateLimits/read":
            return {"rateLimitsByLimitId": {"codex": {
                "limitId": "codex",
                "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1800000000},
                "secondary": {"usedPercent": 60, "windowDurationMins": 10080, "resetsAt": 1800500000},
            }}}
        if method == "thread/start":
            self.threads += 1
            return {"thread": {"id": f"thread-{self.threads}"}}
        if method == "turn/start":
            self._push(params["threadId"], self.turn_script)
            return {"turn": {"id": f"turn-{self.threads}"}}
        return {}

    async def respond(self, request_id, result=None, error=None):
        self.responses.append((request_id, result, error))
        thread_id = next(iter(self.queues), None)
        if thread_id and request_id == "req-1":
            self._push(thread_id, self.resume_script)

    def methods(self):
        return [method for method, _params in self.requests]


def _tool_call(request_id, call_id, tool="search", arguments=None):
    return {"id": request_id, "method": "item/tool/call", "params": {
        "callId": call_id, "tool": tool, "arguments": arguments or {"query": "hello"},
    }}


_FINISH = [
    {"method": "item/completed", "params": {"item": {"type": "agentMessage", "phase": "final_answer", "text": "Done."}}},
    {"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {"total": {
        "inputTokens": 30, "outputTokens": 8, "reasoningOutputTokens": 2, "cachedInputTokens": 10, "totalTokens": 38,
    }}}},
    {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
]


def _service(tmp_path, monkeypatch, server):
    import app.agent.codex_account as account_module

    service = CodexAccountService(tmp_path)
    service.server = server
    monkeypatch.setattr(account_module, "codex_account", service)
    monkeypatch.setattr(account_module, "WORKSPACE_DIR", tmp_path)
    return service


def _run_tool_round(client, messages, response, output="found it"):
    messages.extend(response.provider_messages)
    for call in response.tool_calls:
        messages.append(build_tool_result_message(
            ToolCallResult.from_output(call_id=call.call_id, name=call.tool_name, output=output, fallback_status="ok"),
            provider=client.provider,
        ))


def test_account_provider_appears_alongside_openai_api():
    providers = {item["id"]: item for item in model_options_payload()["providers"]}
    assert providers["openai"]["label"] == "OpenAI API"
    assert providers["codex"]["label"] == "OpenAI account"
    assert providers["codex"]["models"] == providers["openai"]["models"]
    assert LLMSettings(provider="codex", model_name="gpt-6-astra", reasoning_effort="none").reasoning_effort == "low"
    assert LLMSettings(provider="codex", model_name="gpt-6-astra", reasoning_effort="ultra").reasoning_effort == "ultra"
    assert LLMSettings(provider="codex", model_name="gpt-6-luna", reasoning_effort="ultra").reasoning_effort == "max"


def test_mcp_dynamic_tools_use_safe_aliases_and_execute_original_registry_name(monkeypatch, tmp_path):
    from app.agent.codex_account import _codex_tool_name, _history_item
    original = "mcp__Chrome-dev-tools__click"
    alias = _codex_tool_name(original)
    server = FakeServer(turn_script=[_tool_call("req-1", "call_1", tool=alias)])
    _service(tmp_path, monkeypatch, server)
    client = LLMClient("codex", "gpt-6-luna", "")
    response = asyncio.run(client.chat_with_tools(
        [{"role": "user", "content": "click"}],
        [{"name": original, "description": "Click", "parameters": {"type": "object"}}],
        f"Use {original}.",
    ))
    assert response.tool_calls[0].tool_name == original
    start = dict(server.requests)["thread/start"]
    assert start["dynamicTools"][0]["name"] == alias
    assert not alias.startswith("mcp__")
    assert alias in start["baseInstructions"]
    assert _history_item({"type": "function_call", "name": original})["name"] == alias
    assert len(_codex_tool_name("mcp__" + "x" * 100)) <= 64


def test_mcp_alias_instructions_handle_tool_names_that_share_a_prefix(monkeypatch, tmp_path):
    server = FakeServer(turn_script=_FINISH)
    _service(tmp_path, monkeypatch, server)
    names = ["mcp__server__read", "mcp__server__read_file"]
    asyncio.run(LLMClient("codex", "gpt-6-luna", "").chat_with_tools(
        [{"role": "user", "content": "read"}],
        [{"name": name, "parameters": {"type": "object"}} for name in names],
        "Use mcp__server__read or mcp__server__read_file.",
    ))
    assert dict(server.requests)["thread/start"]["baseInstructions"] == (
        "Use monaw_mcp__server__read or monaw_mcp__server__read_file."
    )


def test_tool_calls_come_back_to_monaw_and_resume_the_same_codex_turn(monkeypatch, tmp_path):
    server = FakeServer(turn_script=[_tool_call("req-1", "call_1")], resume_script=_FINISH)
    _service(tmp_path, monkeypatch, server)
    client = LLMClient("codex", "gpt-6-luna", "", reasoning_effort="high")

    async def scenario():
        messages = [{"role": "user", "content": "search for hello"}]
        first = await client.chat_with_tools(messages, TOOLS, "Follow the user request.")
        assert [(call.call_id, call.tool_name, call.arguments) for call in first.tool_calls] == [
            ("call_1", "search", {"query": "hello"}),
        ]
        assert first.provider_messages == [{
            "type": "function_call", "call_id": "call_1", "name": "search", "arguments": '{"query": "hello"}',
        }]
        _run_tool_round(client, messages, first)
        assert messages[-1] == {"type": "function_call_output", "call_id": "call_1", "output": "found it"}
        return await client.chat_with_tools(messages, TOOLS, "Follow the user request.")

    second = asyncio.run(scenario())
    assert second.content == "Done."
    assert second.tool_calls == []
    assert second.usage.source == "codex_account"
    assert second.usage.input_tokens == 30
    assert server.methods().count("thread/start") == 1
    assert server.responses == [("req-1", {
        "contentItems": [{"type": "inputText", "text": "found it"}], "success": True,
    }, None)]
    thread_start = dict(server.requests)["thread/start"]
    assert thread_start["baseInstructions"] == "Follow the user request."
    assert thread_start["dynamicTools"] == [{
        "type": "function", "name": "search", "description": "Search", "inputSchema": {"type": "object"},
    }]
    assert dict(server.requests)["turn/start"]["effort"] == "high"


def test_new_thread_replays_history_as_native_responses_items(monkeypatch, tmp_path):
    server = FakeServer(turn_script=_FINISH)
    _service(tmp_path, monkeypatch, server)
    client = LLMClient("codex", "gpt-6-luna", "")
    messages = [
        {"role": "user", "content": "weather?"},
        {"type": "function_call", "call_id": "call_9", "name": "search", "arguments": '{"query": "weather"}'},
        {"type": "function_call_output", "call_id": "call_9", "output": "sunny"},
        {"role": "assistant", "content": "It is sunny."},
        {"type": "reasoning", "encrypted_content": "from-another-provider"},
        {"role": "user", "content": "and tomorrow?"},
    ]
    response = asyncio.run(client.chat_with_tools(messages, TOOLS, "System."))
    assert response.content == "Done."
    requests = dict(server.requests)
    assert requests["thread/inject_items"]["items"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "weather?"}]},
        {"type": "function_call", "call_id": "call_9", "name": "search", "arguments": '{"query": "weather"}'},
        {"type": "function_call_output", "call_id": "call_9", "output": "sunny"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "It is sunny."}]},
    ]
    assert requests["turn/start"]["input"] == [{"type": "text", "text": "and tomorrow?"}]


def test_changed_tool_list_moves_the_run_to_a_new_thread(monkeypatch, tmp_path):
    server = FakeServer(turn_script=[_tool_call("req-1", "call_1")])
    service = _service(tmp_path, monkeypatch, server)
    client = LLMClient("codex", "gpt-6-luna", "")

    async def scenario():
        messages = [{"role": "user", "content": "search"}]
        first = await client.chat_with_tools(messages, TOOLS, "System.")
        _run_tool_round(client, messages, first)
        server.turn_script = _FINISH
        more_tools = TOOLS + [{"name": "open", "description": "Open", "parameters": {"type": "object"}}]
        result = await client.chat_with_tools(messages, more_tools, "System.")
        await asyncio.gather(*service._cleanup_tasks)
        return result

    result = asyncio.run(scenario())
    assert result.content == "Done."
    assert server.methods().count("thread/start") == 2
    assert ("turn/interrupt", {"threadId": "thread-1", "turnId": "turn-1"}) in server.requests
    injected = [params for method, params in server.requests if method == "thread/inject_items"][0]["items"]
    assert injected[-1] == {"type": "function_call_output", "call_id": "call_1", "output": "found it"}
    assert [params for method, params in server.requests if method == "turn/start"][-1]["input"][0]["type"] == "text"


def test_notes_after_tool_results_ride_along_with_the_last_result(monkeypatch, tmp_path):
    server = FakeServer(turn_script=[_tool_call("req-1", "call_1")], resume_script=_FINISH)
    _service(tmp_path, monkeypatch, server)
    client = LLMClient("codex", "gpt-6-luna", "")

    async def scenario():
        messages = [{"role": "user", "content": "search"}]
        first = await client.chat_with_tools(messages, TOOLS, "System.")
        _run_tool_round(client, messages, first, output="error: timeout")
        messages.append({"role": "user", "content": "Try a narrower query."})
        return await client.chat_with_tools(messages, TOOLS, "System.")

    assert asyncio.run(scenario()).content == "Done."
    assert server.responses[0][1]["contentItems"] == [
        {"type": "inputText", "text": "error: timeout"},
        {"type": "inputText", "text": "Try a narrower query."},
    ]


def test_codex_side_requests_are_refused(monkeypatch, tmp_path):
    approval = {"id": "req-9", "method": "item/commandExecution/requestApproval", "params": {"command": "dir"}}
    server = FakeServer(turn_script=[approval] + _FINISH)
    _service(tmp_path, monkeypatch, server)
    asyncio.run(LLMClient("codex", "gpt-6-luna", "").chat_with_tools(
        [{"role": "user", "content": "hi"}], [], "System.",
    ))
    assert server.responses == [("req-9", {"decision": "decline"}, None)]
    assert default_server_response("item/fileChange/requestApproval") == ({"decision": "decline"}, None)
    result, error = default_server_response("item/somethingNew")
    assert result is None and error["code"] == -32601


def test_reasoning_summary_and_answer_stream_to_the_turn_loop(monkeypatch, tmp_path):
    server = FakeServer(turn_script=[
        {"method": "item/reasoning/summaryTextDelta", "params": {"itemId": "r1", "delta": "Checking the request."}},
        {"method": "item/completed", "params": {"item": {"type": "reasoning", "id": "r1", "summary": ["Checking the request."]}}},
        {"method": "item/agentMessage/delta", "params": {"delta": "Do"}},
        {"method": "item/agentMessage/delta", "params": {"delta": "ne."}},
    ] + _FINISH)
    _service(tmp_path, monkeypatch, server)
    thoughts: list[str] = []
    tokens: list[str] = []

    async def on_summary(delta):
        thoughts.append(delta)

    async def on_token(delta):
        tokens.append(delta)

    response = asyncio.run(LLMClient("codex", "gpt-6-luna", "").chat_with_tools(
        [{"role": "user", "content": "Hello"}], [], stream_callback=on_token, reasoning_callback=on_summary,
    ))
    assert thoughts == ["Checking the request."]
    assert tokens == ["Do", "ne."]
    assert response.reasoning_content == "Checking the request."
    assert dict(server.requests)["turn/start"]["summary"] == "auto"


def test_signed_out_account_is_reported(monkeypatch, tmp_path):
    server = FakeServer(turn_script=_FINISH)
    server.account = None
    _service(tmp_path, monkeypatch, server)
    try:
        asyncio.run(LLMClient("codex", "gpt-6-luna", "").chat_with_tools([{"role": "user", "content": "hi"}], []))
    except ValueError as exc:
        assert "not connected" in str(exc)
    else:
        raise AssertionError("expected a sign-in error")
    assert "thread/start" not in server.methods()


def test_model_catalog_copy_exposes_tools_directly(tmp_path):
    assert write_model_catalog(tmp_path) is None
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [{
        "slug": "gpt-6-luna", "tool_mode": "code_mode_only", "multi_agent_version": "v2",
        "apply_patch_tool_type": "freeform", "experimental_supported_tools": ["clock"],
        "supports_search_tool": True, "context_window": 272000,
    }]}), encoding="utf-8")
    path = write_model_catalog(tmp_path)
    model = json.loads(path.read_text(encoding="utf-8"))["models"][0]
    assert model == {
        "slug": "gpt-6-luna", "experimental_supported_tools": [],
        "supports_search_tool": False, "context_window": 272000,
    }


def test_app_server_launch_switches_off_codex_tools(monkeypatch, tmp_path):
    launched: list[list[str]] = []

    async def fake_exec(*args, **kwargs):
        launched.append(list(args))
        assert kwargs["env"]["CODEX_HOME"] == str(tmp_path)
        assert kwargs["env"]["OPENAI_API_KEY"] == ""
        raise RuntimeError("stop here")

    monkeypatch.setattr(codex_app_server, "_codex_binary", lambda: (Path("codex.exe"), None))
    monkeypatch.setattr(codex_app_server.asyncio, "create_subprocess_exec", fake_exec)
    server = codex_app_server.CodexAppServer(tmp_path)
    try:
        asyncio.run(server.ensure_started())
    except RuntimeError:
        pass
    args = launched[0]
    for flag in ("features.shell_tool=false", "features.unified_exec=false", "features.code_mode=false",
                 "features.multi_agent=false", 'web_search="disabled"', "include_environment_context=false"):
        assert flag in args
    assert args[-3:] == ["app-server", "--listen", "stdio://"]


def test_account_status_loads_usage_windows_and_reset_timestamps(tmp_path):
    service = CodexAccountService(tmp_path)
    service.server = FakeServer()
    status = asyncio.run(service.status())
    assert status["connected"] is True
    assert status["plan"] == "plus"
    assert [(item["used_percent"], item["resets_at"]) for item in status["limits"]] == [
        (25.0, 1800000000), (60.0, 1800500000),
    ]
    assert asyncio.run(service.status(include_limits=False))["limits"] == []
    assert _rate_limit_windows({"rateLimits": {
        "limitId": "codex", "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": None},
    }})[0]["used_percent"] == 0


def test_account_routes_do_not_expose_credentials(monkeypatch):
    import app.api.routes.openai_account as routes

    async def status():
        return {"connected": True, "plan": "plus"}

    async def login():
        return {"auth_url": "https://auth.openai.com/example", "login_id": "one"}

    async def logout():
        return None

    monkeypatch.setattr(routes.codex_account, "status", status)
    monkeypatch.setattr(routes.codex_account, "start_login", login)
    monkeypatch.setattr(routes.codex_account, "logout", logout)
    assert asyncio.run(routes.account_status()) == {"connected": True, "plan": "plus"}
    assert asyncio.run(routes.account_login())["auth_url"].startswith("https://")
    assert asyncio.run(routes.account_logout())["connected"] is False
