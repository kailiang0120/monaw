import asyncio
import json
import time
from types import SimpleNamespace

import httpx

from app.agent.llm_client import LLMResponse, ToolCallRequest
from app.agent.settings_store import (
    AgentSettings,
    DecisionModelSettings,
    build_runtime_namespace,
    merge_agent_settings,
)
from app.agent.skill_loader import load_tools
from app.agent.tool_registry import ToolRegistry
from app.agent.tool_selection import (
    LayaClient,
    ToolSelectionDecision,
    ToolSelector,
    build_tool_selector,
    evaluate_selection,
    select_domains,
    tool_domain,
)
from app.agent.turn_loop import TurnLoop


def _tool(name: str, skill: str = "", **extra) -> dict:
    tool = {
        "name": name,
        "description": f"{name} tool",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
        "callable": lambda text="": f"{name}:{text}",
        "domain": "general",
        "execution_mode": "sync_stateless",
        "affinity_group": None,
        "metadata": {"skill": skill} if skill else {},
    }
    tool.update(extra)
    return tool


VISIBLE_TOOLS = [
    _tool("calculator", "core"),
    _tool("memory_search", "memory"),
    _tool("tool_search"),
    _tool("exec_command", "exec"),
    _tool("fs_read", "filesystem"),
    _tool("fs_write", "filesystem"),
    _tool("web_search", "web-search"),
    _tool("browser_open", "browser-use"),
    _tool("mcp_notes__find", mcp_bridge={"server_name": "notes"}),
]


def _laya_response(probabilities: dict[str, float], *, confidence: float = 0.8) -> dict:
    top = max(probabilities, key=probabilities.get)
    return {
        "model": "laya",
        "answers": {
            "capability": {
                "type": "choice",
                "choice": top,
                "probabilities": probabilities,
                "confidence": confidence,
                "answer_confidence": probabilities[top],
            }
        },
        "usage": {"input_tokens": 120, "output_tokens": 0},
        "routing": {"model": "laya", "reason": "english"},
    }


def _config(**overrides) -> DecisionModelSettings:
    return DecisionModelSettings(enabled=True, **overrides)


def _selector(handler, **overrides) -> ToolSelector:
    config = _config(**overrides)
    client = LayaClient(
        config.endpoint,
        api_key="test-key",
        timeout_seconds=config.timeout_ms / 1000,
        transport=httpx.MockTransport(handler),
    )
    return ToolSelector(
        config,
        skill_summaries={"exec": "Run local commands.", "web-search": "Search the web for current information."},
        mcp_descriptions={"notes": "Personal notes server."},
        client=client,
    )


def test_tool_domain_prefers_mcp_server_then_skill_then_builtin():
    assert tool_domain(_tool("mcp_notes__find", "mcp-bridge", mcp_bridge={"server_name": "notes"})) == "mcp:notes"
    assert tool_domain(_tool("fs_read", "filesystem")) == "filesystem"
    assert tool_domain(_tool("tool_search")) == "tool-search"
    assert tool_domain(_tool("legacy")) == "builtin"
    assert tool_domain(None) == ""


def test_select_domains_stops_at_coverage_or_cap():
    probabilities = {"exec": 0.6, "filesystem": 0.3, "web-search": 0.07, "browser-use": 0.03}
    assert select_domains(probabilities, coverage=0.9, max_domains=4) == ["exec", "filesystem"]
    assert select_domains(probabilities, coverage=0.99, max_domains=2) == ["exec", "filesystem"]
    assert select_domains(probabilities, coverage=0.5, max_domains=4) == ["exec"]


def test_options_cover_unpinned_domains_only():
    selector = _selector(lambda request: httpx.Response(500))
    options = {option.domain: option for option in selector.build_options(VISIBLE_TOOLS)}

    assert set(options) == {"browser-use", "exec", "filesystem", "mcp:notes", "web-search"}
    assert options["exec"].description == "Run local commands."
    assert options["mcp:notes"].description == "Personal notes server."
    assert options["filesystem"].tool_names == ("fs_read", "fs_write")
    assert options["browser-use"].description == "Tools: browser_open"


def test_decide_sends_laya_request_and_selects_domains():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_laya_response({
            "browser-use": 0.03, "exec": 0.55, "filesystem": 0.35,
            "mcp:notes": 0.03, "web-search": 0.04,
        }))

    selector = _selector(handler, model="laya-multilingual")
    history = [
        {"role": "user", "content": "List the repo files"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]},
        {"role": "assistant", "content": "Here are the files."},
    ]
    decision = asyncio.run(selector.decide("Now run the tests", visible_tools=VISIBLE_TOOLS, history=history))

    assert seen["url"] == "http://127.0.0.1:8421/v1/systemone"
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["model"] == "laya-multilingual"
    assert seen["body"]["state"] == {
        "latest_request": "Now run the tests",
        "previous_user_message": "List the repo files",
        "previous_assistant_reply": "Here are the files.",
    }
    question = seen["body"]["questions"]["capability"]
    assert question["type"] == "choice"
    assert set(question["criteria"]) == {"browser-use", "exec", "filesystem", "mcp:notes", "web-search"}

    assert decision.status == "ok"
    assert decision.full_fallback is False
    assert decision.top_domain == "exec"
    assert decision.selected_domains == ["exec", "filesystem"]
    assert decision.confidence == 0.8
    assert decision.kept_domains(tool_domain(tool) for tool in VISIBLE_TOOLS) == {
        "core", "memory", "tool-search", "exec", "filesystem",
    }


def test_full_request_is_sent_without_the_previous_turn():
    history = [
        {"role": "user", "content": "Schedule a task for every weekday at 9am"},
        {"role": "assistant", "content": "Scheduled."},
    ]

    full = ToolSelector.build_state("Find today's USD to MYR exchange rate and save it to a file", history)
    follow_up = ToolSelector.build_state("yes, do it", history)

    assert full == {"latest_request": "Find today's USD to MYR exchange rate and save it to a file"}
    assert follow_up["previous_user_message"] == "Schedule a task for every weekday at 9am"
    assert follow_up["previous_assistant_reply"] == "Scheduled."


def test_low_confidence_falls_back_to_every_tool():
    selector = _selector(
        lambda request: httpx.Response(200, json=_laya_response({
            "browser-use": 0.2, "exec": 0.3, "filesystem": 0.2,
            "mcp:notes": 0.1, "web-search": 0.2,
        })),
        min_confidence=0.4,
    )
    decision = asyncio.run(selector.decide("do something", visible_tools=VISIBLE_TOOLS))

    assert decision.status == "low_confidence"
    assert decision.full_fallback is True
    visible = {tool_domain(tool) for tool in VISIBLE_TOOLS}
    assert decision.kept_domains(visible) == visible


def test_unreachable_server_cools_down_instead_of_retrying_every_turn():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("connection refused", request=request)

    selector = _selector(handler)
    first = asyncio.run(selector.decide("hello", visible_tools=VISIBLE_TOOLS))
    second = asyncio.run(selector.decide("hello again", visible_tools=VISIBLE_TOOLS))

    assert first.status == "error"
    assert "ConnectError" in first.error
    assert second.status == "unavailable"
    assert len(calls) == 1


def test_bad_request_reports_error_without_cooldown():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(422, json={"detail": "question 'capability' is invalid"})

    selector = _selector(handler)
    first = asyncio.run(selector.decide("hello", visible_tools=VISIBLE_TOOLS))
    asyncio.run(selector.decide("hello", visible_tools=VISIBLE_TOOLS))

    assert first.status == "error"
    assert first.error == "HTTP 422: question 'capability' is invalid"
    assert len(calls) == 2


def test_malformed_response_is_an_error_not_an_exception():
    selector = _selector(lambda request: httpx.Response(200, json={"answers": {}}))
    decision = asyncio.run(selector.decide("hello", visible_tools=VISIBLE_TOOLS))

    assert decision.status == "error"
    assert "no 'capability' answer" in decision.error
    assert decision.full_fallback is True


def test_single_selectable_domain_is_skipped_without_calling_the_model():
    calls = []
    selector = _selector(lambda request: calls.append(request) or httpx.Response(500))
    decision = asyncio.run(selector.decide("run it", visible_tools=VISIBLE_TOOLS[:4]))

    assert decision.status == "skipped"
    assert decision.options == ["exec"]
    assert calls == []


def test_empty_request_is_skipped_without_calling_the_model():
    calls = []
    selector = _selector(lambda request: calls.append(request) or httpx.Response(500))
    decision = asyncio.run(selector.decide("   ", visible_tools=VISIBLE_TOOLS))

    assert decision.status == "skipped"
    assert calls == []


def test_evaluate_selection_reports_coverage_and_savings():
    registry = ToolRegistry(VISIBLE_TOOLS)
    decision = ToolSelectionDecision(
        status="ok",
        selected_domains=["exec"],
        pinned_domains=["core", "memory", "tool-search"],
        top_domain="exec",
        top_probability=0.7,
        full_fallback=False,
    )

    covered = evaluate_selection(
        decision,
        visible_tools=VISIBLE_TOOLS,
        used_tool_names=["exec_command", "memory_search", "final_answer"],
        lookup_tool=registry.get_tool,
    )
    assert covered["covered"] is True
    assert covered["used_domains"] == ["exec", "memory"]
    assert covered["missed_domains"] == []
    assert covered["top_domain_used"] is True
    assert covered["visible_tool_count"] == len(VISIBLE_TOOLS)
    assert covered["kept_tool_count"] == 4
    assert covered["dropped_schema_chars"] > 0

    missed = evaluate_selection(
        decision,
        visible_tools=VISIBLE_TOOLS,
        used_tool_names=["fs_write"],
        lookup_tool=registry.get_tool,
    )
    assert missed["covered"] is False
    assert missed["missed_domains"] == ["filesystem"]


def test_evaluate_selection_does_not_score_failed_decisions():
    registry = ToolRegistry(VISIBLE_TOOLS)
    metadata = evaluate_selection(
        ToolSelectionDecision(status="error", error="timeout"),
        visible_tools=VISIBLE_TOOLS,
        used_tool_names=["fs_write"],
        lookup_tool=registry.get_tool,
    )

    assert metadata["covered"] is None
    assert metadata["missed_domains"] == []
    assert metadata["kept_tool_count"] == len(VISIBLE_TOOLS)


def _runtime_settings(**decision) -> SimpleNamespace:
    base = SimpleNamespace(
        model_provider="openai",
        model_name="gpt-5.6-luna",
        openai_api_key="key",
        google_api_key="",
        tavily_api_key="",
        laya_api_key="laya-key",
        reasoning_effort="medium",
        allow_arbitrary_app_paths=False,
    )
    agent_settings = AgentSettings()
    if decision:
        agent_settings = merge_agent_settings(agent_settings, {"decision_model": decision})
    return build_runtime_namespace(base, agent_settings)


def test_decision_model_is_on_by_default_but_waits_for_the_managed_install(monkeypatch):
    config = AgentSettings().decision_model
    assert config.enabled is True
    assert config.managed is True
    assert config.endpoint == "http://127.0.0.1:8421"

    monkeypatch.setattr("app.agent.tool_selection.managed_laya_executable", lambda: None)
    assert build_tool_selector(_runtime_settings(), []) is None

    monkeypatch.setattr("app.agent.tool_selection.managed_laya_executable", lambda: "laya-serve.exe")
    assert build_tool_selector(_runtime_settings(), []) is not None
    assert build_tool_selector(_runtime_settings(enabled=False), []) is None


def test_build_tool_selector_from_settings():
    skills = [SimpleNamespace(name="exec", summary="Run local commands.", description="")]
    selector = build_tool_selector(
        _runtime_settings(managed=False, endpoint="http://localhost:9000/", timeout_ms=1500), skills
    )

    assert selector is not None
    assert selector.client.endpoint == "http://localhost:9000"
    assert selector.client.api_key == "laya-key"
    assert selector.client._trust_env is False
    assert selector.timeout_seconds == 1.5
    assert selector.skill_summaries["exec"] == "Run local commands."
    assert build_tool_selector(_runtime_settings(managed=False, endpoint="localhost:9000"), skills) is None


def test_load_tools_tags_each_tool_with_its_skill(tmp_path):
    skill_dir = tmp_path / "good_skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: good-skill\ndescription: Good\n---\nGood skill\n", encoding="utf-8")
    (skill_dir / "tools.py").write_text(
        "def register_tools(registry, settings=None):\n"
        "    registry.register({'name':'good_tool','description':'ok','parameters':{'type':'object','properties':{},'required':[]},"
        "'callable':lambda:'ok','domain':'general','execution_mode':'sync_stateless','affinity_group':None,"
        "'metadata':{'parallel_safe': True}})\n",
        encoding="utf-8",
    )

    _skills, registry = load_tools(_runtime_settings(), skills_dir=tmp_path)
    tool = registry.get_tool("good_tool")

    assert tool["metadata"]["skill"] == "good-skill"
    assert tool["metadata"]["parallel_safe"] is True
    assert tool_domain(tool) == "good-skill"
    assert tool_domain(registry.get_tool("tool_search")) == "tool-search"


class _Memory:
    def __init__(self) -> None:
        self.added_messages = []

    def get_or_create(self, _conversation_id, title=""):
        return {"title": title}

    def set_task_goal(self, _conversation_id, goal):
        return None

    def set_active_task(self, _conversation_id, _task):
        return None

    async def ensure_context_fits(self, _conversation_id, _system_prompt="", **_kwargs):
        return None

    def build_llm_messages(self, _conversation_id):
        return []

    def add_tool_outcome(self, _conversation_id, tool_name, result):
        return None

    async def add_message_and_maybe_summarize(self, _conversation_id, role, content):
        self.added_messages.append((role, content))

    def clear_task_progress(self, _conversation_id):
        return None


class _Observability:
    def __init__(self) -> None:
        self.events = []

    def start_run(self, **kwargs):
        return "obs-run"

    def log_event(self, **kwargs):
        self.events.append(kwargs)
        return "evt"

    def log_error(self, **kwargs):
        return "err"

    def finish_run(self, **kwargs):
        return None

    def finish_open_run_for_conversation(self, **kwargs):
        return None


class _ExecLLM:
    def __init__(self) -> None:
        self.tool_counts = []

    async def chat_with_tools(self, messages, tools, system_prompt="", **kwargs):
        self.tool_counts.append(len(tools))
        if len(self.tool_counts) == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(call_id="c1", tool_name="exec_command", arguments={"text": "pytest"})],
                finish_reason="tool_calls",
            )
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(call_id="f", tool_name="final_answer", arguments={"answer": "Tests pass."})],
            finish_reason="tool_calls",
        )


def _run_turn(loop: TurnLoop, message: str = "run the tests") -> list[dict]:
    async def collect():
        return [event async for event in loop.run(message, "conv-select", system_prompt="system")]

    return asyncio.run(collect())


def _shadow_events(observability: _Observability) -> list[dict]:
    return [event for event in observability.events if event["event_type"] == "tool_selection_shadow"]


def test_turn_loop_logs_shadow_decision_and_keeps_every_tool():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_laya_response({
            "browser-use": 0.03, "exec": 0.8, "filesystem": 0.1,
            "mcp:notes": 0.03, "web-search": 0.04,
        }))

    llm = _ExecLLM()
    observability = _Observability()
    loop = TurnLoop(
        llm_client=llm,
        registry=ToolRegistry(VISIBLE_TOOLS),
        memory=_Memory(),
        observability=observability,
        tool_selector=_selector(handler),
    )
    events = _run_turn(loop)

    assert events[-1]["event"] == "done"
    assert llm.tool_counts == [len(VISIBLE_TOOLS), len(VISIBLE_TOOLS)]
    [shadow] = _shadow_events(observability)
    assert shadow["status"] == "ok"
    metadata = shadow["metadata"]
    assert metadata["mode"] == "shadow"
    assert metadata["top_domain"] == "exec"
    assert metadata["used_domains"] == ["exec"]
    assert metadata["covered"] is True
    assert metadata["kept_domains"] == ["core", "exec", "filesystem", "memory", "tool-search"]
    assert metadata["kept_tool_count"] == 6
    assert "latest_request" not in json.dumps(metadata)


def test_tools_hidden_until_tool_search_are_still_options():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["criteria"] = json.loads(request.content)["questions"]["capability"]["criteria"]
        return httpx.Response(200, json=_laya_response({
            "browser-use": 0.02, "exec": 0.03, "filesystem": 0.02,
            "mcp:notes": 0.02, "scheduling": 0.9, "web-search": 0.01,
        }))

    class ScheduleLLM(_ExecLLM):
        async def chat_with_tools(self, messages, tools, system_prompt="", **kwargs):
            self.tool_counts.append(len(tools))
            name = "scheduled_task_create" if len(self.tool_counts) == 1 else "final_answer"
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(call_id=f"c{len(self.tool_counts)}", tool_name=name, arguments={})],
                finish_reason="tool_calls",
            )

    hidden = _tool("scheduled_task_create", "scheduling", visible_to_model=False)
    observability = _Observability()
    llm = ScheduleLLM()
    loop = TurnLoop(
        llm_client=llm,
        registry=ToolRegistry([*VISIBLE_TOOLS, hidden]),
        memory=_Memory(),
        observability=observability,
        tool_selector=_selector(handler),
    )
    _run_turn(loop, "Remind me every weekday at 9am to check my email")

    assert "scheduling" in seen["criteria"]
    assert llm.tool_counts[0] == len(VISIBLE_TOOLS)
    [shadow] = _shadow_events(observability)
    metadata = shadow["metadata"]
    assert "scheduling" in metadata["kept_domains"]
    assert metadata["visible_tool_count"] == len(VISIBLE_TOOLS)


def test_slow_decision_model_does_not_hold_the_turn():
    class SlowSelector(ToolSelector):
        async def decide(self, user_request, **kwargs):
            await asyncio.sleep(30)

    selector = SlowSelector(_config(timeout_ms=100), client=LayaClient("http://127.0.0.1:1"))
    observability = _Observability()
    loop = TurnLoop(
        llm_client=_ExecLLM(),
        registry=ToolRegistry(VISIBLE_TOOLS),
        memory=_Memory(),
        observability=observability,
        tool_selector=selector,
    )
    started = time.monotonic()
    events = _run_turn(loop)

    assert events[-1]["event"] == "done"
    assert time.monotonic() - started < 5
    [shadow] = _shadow_events(observability)
    assert shadow["status"] == "timeout"
    assert shadow["metadata"]["covered"] is None


def test_turn_loop_without_selector_logs_nothing():
    observability = _Observability()
    loop = TurnLoop(
        llm_client=_ExecLLM(),
        registry=ToolRegistry(VISIBLE_TOOLS),
        memory=_Memory(),
        observability=observability,
    )
    _run_turn(loop)

    assert _shadow_events(observability) == []


def _shadow_metadata(probabilities: dict[str, float], used: list[str]) -> dict:
    registry = ToolRegistry(VISIBLE_TOOLS)
    top = max(probabilities, key=probabilities.get)
    decision = ToolSelectionDecision(
        status="ok",
        probabilities=probabilities,
        selected_domains=select_domains(probabilities, coverage=0.9, max_domains=4),
        pinned_domains=["core", "memory", "tool-search"],
        top_domain=top,
        top_probability=probabilities[top],
        latency_ms=40,
        full_fallback=False,
    )
    return evaluate_selection(decision, visible_tools=VISIBLE_TOOLS, used_tool_names=used, lookup_tool=registry.get_tool)


def test_report_replays_thresholds_from_stored_probabilities(tmp_path):
    import sqlite3

    from app.agent.tool_selection_report import format_summary, load_shadow_events, replay_policy, summarize

    events = [
        _shadow_metadata({"exec": 0.7, "filesystem": 0.25, "web-search": 0.05}, ["exec_command", "fs_write"]),
        _shadow_metadata({"web-search": 0.9, "exec": 0.1}, ["web_search"]),
        {"status": "error", "error": "timeout"},
    ]
    db = tmp_path / "observability.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE observability_events (event_type TEXT, metadata_json TEXT, created_at TEXT)")
    conn.executemany(
        "INSERT INTO observability_events VALUES (?, ?, ?)",
        [("tool_selection_shadow", json.dumps(event), f"2026-10-18T00:00:0{index}") for index, event in enumerate(events)]
        + [("llm_call_started", "{}", "2026-10-18T00:00:09")],
    )
    conn.commit()
    conn.close()

    loaded = load_shadow_events(db)
    assert len(loaded) == 3

    strict = replay_policy(loaded, coverage=0.7, min_confidence=0.0, max_domains=4)
    assert strict["decided"] == 2
    assert strict["covered_rate"] == 0.5  # exec alone misses the filesystem write
    loose = replay_policy(loaded, coverage=0.95, min_confidence=0.0, max_domains=4)
    assert loose["covered_rate"] == 1.0
    assert loose["kept_tool_share"] > strict["kept_tool_share"]
    fallback = replay_policy(loaded, coverage=0.7, min_confidence=0.95, max_domains=4)
    assert fallback["fallback_rate"] == 1.0
    assert fallback["kept_tool_share"] == 1.0

    summary = summarize(loaded)
    assert summary["statuses"] == {"ok": 2, "error": 1}
    assert summary["latency_ms_p50"] == 40
    assert summary["top_domain_used_rate"] == 1.0
    assert "coverage  min_conf" in format_summary(summary)
