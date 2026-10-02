"""Regression tests for memory continuity and context management."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.agent.context_usage import (
    build_context_usage_report,
    count_text_tokens,
    estimate_message_tokens,
    model_compaction_threshold,
    model_context_token_limit,
    token_estimation_method,
)
import app.agent.memory_manager as memory_manager_mod
import app.api.routes.conversations as routes_mod
from app.agent.database import Database
from app.agent.memory_manager import MemoryManager
from app.agent.skill_loader import SkillSpec
from app.agent.state import (
    ConversationState,
    ExecutionPlan,
    IntentResult,
    PlanStep,
    StepResult,
    TaskState,
)
from app.main import app


class RecordingLLM:
    def __init__(self, reply: str = "compacted summary") -> None:
        self.reply = reply
        self.calls: list[dict] = []
        self.provider = "openai"
        self.model_name = "gpt-5.6-luna"

    async def chat(self, messages, system_prompt: str = "", stream_callback=None):
        self.calls.append({
            "messages": messages,
            "system_prompt": system_prompt,
        })
        return self.reply


class ExplodingLLM:
    async def chat(self, *args, **kwargs):
        raise AssertionError("LLM fallback should not be used for this classification")


class FailingCompactionLLM(RecordingLLM):
    async def chat(self, messages, system_prompt: str = "", stream_callback=None):
        self.calls.append({
            "messages": messages,
            "system_prompt": system_prompt,
        })
        raise RuntimeError("compaction unavailable")


@pytest.fixture(autouse=True)
def isolated_memory_db(monkeypatch, tmp_path):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    monkeypatch.setattr(memory_manager_mod, "get_db", lambda: db)
    monkeypatch.setattr(routes_mod, "get_db", lambda: db)
    memory_manager_mod.delete_memory()
    yield db
    memory_manager_mod.delete_memory()
    db.close()


def _patch_memory_dirs(monkeypatch, tmp_path: Path) -> None:
    runtime_dir = tmp_path / ".runtime"
    conversations_dir = runtime_dir / "conversations"
    monkeypatch.setattr(memory_manager_mod, "_RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(memory_manager_mod, "_CONVERSATIONS_DIR", conversations_dir)


def test_conversation_state_persists_task_goal_and_steps():
    state = ConversationState(
        conversation_id="conv-1",
        task_goal="Download Node.js and organize Downloads",
        pending_steps=[{"step_id": "step_2", "description": "Organize files", "status": "pending"}],
        completed_steps=["Downloaded installer"],
    )

    payload = state.model_dump()
    restored = ConversationState.model_validate(payload)

    assert restored.task_goal == "Download Node.js and organize Downloads"
    assert restored.pending_steps == [{"step_id": "step_2", "description": "Organize files", "status": "pending"}]
    assert restored.completed_steps == ["Downloaded installer"]


def test_steering_messages_persist_in_the_same_chat_and_reach_next_turn(monkeypatch, tmp_path):
    _patch_memory_dirs(monkeypatch, tmp_path)
    manager = MemoryManager(RecordingLLM())
    manager.get_or_create("conv-steer-history")
    asyncio.run(manager.persist_turn(
        "conv-steer-history", "Summarize", "Here is the table", tool_calls=[],
        steering_messages=["Use a table", "Keep it short"],
    ))
    user_messages = [item["content"] for item in manager.build_llm_messages("conv-steer-history") if item["role"] == "user"]
    assert user_messages == ["Summarize", "Use a table", "Keep it short"]
    assert not any(item.get("content") == "Use a table" for item in manager.build_llm_messages("another-chat"))


def test_uploaded_images_survive_reopen_and_steering_without_cross_session_access(monkeypatch, tmp_path):
    from app.agent import response_attachments as attachment_module
    from app.agent.response_attachments import register_attachment_path, public_attachment_payload
    from app.agent.run_context import (
        set_current_control_session_id, reset_current_control_session_id,
        set_current_principal_id, reset_current_principal_id,
    )
    monkeypatch.setattr(attachment_module, "_ATTACHMENT_REGISTRY_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(attachment_module, "_ATTACHMENT_REGISTRY_LOADED", True)
    monkeypatch.setattr(attachment_module, "_ATTACHMENT_REGISTRY", {})
    monkeypatch.setattr(attachment_module, "_is_read_allowed_by_policy", lambda _path: True)
    paths = [tmp_path / "original.png", tmp_path / "steered.png"]
    for path in paths:
        path.write_bytes(b"image")
    references = [public_attachment_payload(register_attachment_path(
        f"history-image-{index}", path, control_session_id="owner", principal_id="owner", conversation_id="image-chat",
    )) for index, path in enumerate(paths)]
    manager = MemoryManager(RecordingLLM())
    asyncio.run(manager.persist_turn(
        "image-chat", "Analyze the image", "Compared them", tool_calls=[],
        user_attachments=[references[0]], steering_messages=["Compare with this"], steering_attachments=[[references[1]]],
    ))
    session_token = set_current_control_session_id("owner")
    principal_token = set_current_principal_id("owner")
    try:
        reopened = MemoryManager(RecordingLLM()).build_llm_messages("image-chat")
        image_paths = [image["path"] for message in reopened for image in message.get("images", [])]
        assert image_paths == [str(path.resolve()) for path in paths]
        foreign_session = set_current_control_session_id("other-session")
        try:
            assert not any(message.get("images") for message in manager.build_llm_messages("image-chat"))
        finally:
            reset_current_control_session_id(foreign_session)
        paths[0].unlink()
        remaining = manager.build_llm_messages("image-chat")
        assert len([image for message in remaining for image in message.get("images", [])]) == 1
    finally:
        reset_current_control_session_id(session_token)
        reset_current_principal_id(principal_token)


def test_failed_turn_restores_bounded_tool_evidence_for_continuation():
    manager = MemoryManager(RecordingLLM())
    raw_result = "Start: saved report " + "detail " * 4000 + " End: report.pdf"
    asyncio.run(manager.persist_turn(
        "failed-tool-evidence", "Create a report", "Paused after an internal error", status="error",
        tool_calls=[{"tool_name": "fs_write", "input": '{"path":"report.pdf"}', "output": raw_result, "status": "complete"}],
    ))
    history = MemoryManager(RecordingLLM()).build_llm_messages("failed-tool-evidence")
    checkpoint = history[-1]["content"]
    assert "Interrupted task tool checkpoint" in checkpoint
    assert '"path":"report.pdf"' in checkpoint
    assert "Start: saved report" in checkpoint and "End: report.pdf" in checkpoint
    assert len(checkpoint) < 3000
    assistant = manager._db.get_messages("failed-tool-evidence")[-1]
    assert manager._db.get_tool_calls_for_message(assistant["id"])[0]["output"] == raw_result


def test_memory_manager_builds_structured_context_and_history(monkeypatch, tmp_path):
    _patch_memory_dirs(monkeypatch, tmp_path)
    llm = RecordingLLM()
    manager = MemoryManager(llm)
    state = manager.get_or_create("conv-structured", title="Structured")

    state.task_goal = "Download Node.js and organize Downloads"
    state.pending_steps = [
        {"step_id": "step_3", "description": "Move PDFs to Documents/PDFs", "status": "pending"},
        {"step_id": "step_4", "description": "Verify files are organized", "status": "pending"},
    ]
    state.completed_steps = ["Downloaded Node.js installer"]
    state.summary = "Downloaded the installer to Downloads and started planning the cleanup."
    state.recent_messages = [
        {"role": "user", "content": "Download Node.js and organize my Downloads folder"},
        {"role": "assistant", "content": "I have downloaded the installer."},
    ]
    state.tool_outcomes = [
        {"tool": "download_file", "result": "Saved installer to C:\\Users\\me\\Downloads\\node.msi"},
    ]

    context = manager.get_context("conv-structured")
    history = manager.build_llm_messages("conv-structured")
    usage = manager.estimate_and_report("conv-structured", system_prompt="System prompt")

    assert "## Active Goal" in context
    assert "Download Node.js and organize Downloads" in context
    assert "## Remaining Steps" in context
    assert "Move PDFs to Documents/PDFs" in context
    assert "## What Happened So Far (Summary)" not in context
    assert "## Recent Messages" not in context
    assert "## Recent Tool Results" in context
    assert history[0]["role"] == "assistant"
    assert "Earlier conversation summary" in history[0]["content"]
    assert history[-1]["role"] == "assistant"
    assert usage["limit"] == 256000
    assert usage["compaction_at"] == 236800
    assert usage["used"] > 0
    assert usage["percentage"] >= 0


def test_build_llm_messages_refreshes_persisted_history_for_reopened_chat(monkeypatch, tmp_path):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    db.create_conversation("conv-reopen", "Reopened")
    monkeypatch.setattr(memory_manager_mod, "get_db", lambda: db)

    manager = MemoryManager(RecordingLLM())
    state = manager.get_or_create("conv-reopen", title="Reopened")
    state.all_messages = []
    state.recent_messages = []

    db.add_message("conv-reopen", "user", "Remember that my target country is Malaysia.")
    db.add_message("conv-reopen", "assistant", "Noted, Malaysia is the target country.")

    history = manager.build_llm_messages("conv-reopen")

    assert any("target country is Malaysia" in item["content"] for item in history)
    assert any("Malaysia is the target country" in item["content"] for item in history)


def test_manual_compact_replaces_prior_raw_history_with_checkpoint(monkeypatch, tmp_path):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    db.create_conversation("conv-manual-compact", "Manual compact")
    monkeypatch.setattr(memory_manager_mod, "get_db", lambda: db)

    db.add_message("conv-manual-compact", "user", "Raw old user line that should be replaced.")
    db.add_message("conv-manual-compact", "assistant", "Raw old assistant line that should be replaced.")
    manager = MemoryManager(RecordingLLM(reply="Current goal: preserve the compacted facts."))

    result = asyncio.run(manager.compact_conversation("conv-manual-compact"))
    db.add_message("conv-manual-compact", "user", "New request after compact.")
    history = manager.build_llm_messages("conv-manual-compact")
    joined = "\n".join(item["content"] for item in history)

    assert result["status"] == "compacted"
    assert db.get_conversation_compaction("conv-manual-compact")["source_message_id"] == 2
    assert "Compacted Conversation Context" in joined
    assert "Current goal: preserve the compacted facts." in joined
    assert "New request after compact." in joined
    assert "Raw old user line that should be replaced." not in joined
    assert "Raw old assistant line that should be replaced." not in joined


def test_manual_compact_failure_keeps_existing_history(monkeypatch, tmp_path):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    db.create_conversation("conv-manual-compact-fail", "Manual compact fail")
    monkeypatch.setattr(memory_manager_mod, "get_db", lambda: db)

    db.add_message("conv-manual-compact-fail", "user", "First raw line should stay visible.")
    db.add_message("conv-manual-compact-fail", "assistant", "Second raw line should stay visible.")
    manager = MemoryManager(FailingCompactionLLM())

    result = asyncio.run(manager.compact_conversation("conv-manual-compact-fail"))
    history = manager.build_llm_messages("conv-manual-compact-fail")
    joined = "\n".join(item["content"] for item in history)

    assert result["status"] == "failed"
    assert result["source_message_id"] == 0
    assert db.get_conversation_compaction("conv-manual-compact-fail") is None
    assert "First raw line should stay visible." in joined
    assert "Second raw line should stay visible." in joined


def test_memory_manager_compaction_prompt_preserves_task_critical_facts(monkeypatch, tmp_path):
    _patch_memory_dirs(monkeypatch, tmp_path)
    llm = RecordingLLM(reply="Compacted summary with task facts preserved.")
    manager = MemoryManager(llm)
    state = manager.get_or_create("conv-compact", title="Compact")

    state.task_goal = "Download Node.js installer and organize Downloads"
    state.pending_steps = [{"step_id": "step_2", "description": "Organize Downloads", "status": "pending"}]
    state.summary = "Installer URL was https://nodejs.org and the file was saved to C:\\Users\\me\\Downloads."
    state.recent_messages = [
        {"role": "user", "content": "Download Node.js from https://nodejs.org"},
        {"role": "assistant", "content": "Downloaded node.msi to C:\\Users\\me\\Downloads"},
        {"role": "user", "content": "Now organize Downloads"},
        {"role": "assistant", "content": "Planning the file moves now."},
        {"role": "user", "content": "Keep going"},
        {"role": "assistant", "content": "I still need to organize the files."},
    ]
    db = manager._db
    db.create_conversation("conv-compact", "Compact")
    for message in state.recent_messages:
        db.add_message("conv-compact", message["role"], message["content"])

    compacted = asyncio.run(manager._compact("conv-compact"))
    assert compacted is True

    assert db.get_conversation_compaction("conv-compact") is not None
    assert state.summary == ""


def test_auto_compaction_stops_when_measured_usage_does_not_improve(monkeypatch):
    manager = MemoryManager(RecordingLLM())
    manager.get_or_create("conv-no-progress")
    monkeypatch.setattr(memory_manager_mod, "model_compaction_threshold", lambda _llm: 10)
    monkeypatch.setattr(manager, "estimate_context_tokens", lambda *args, **kwargs: 100)
    calls = 0

    async def compact_without_progress(_conversation_id):
        nonlocal calls
        calls += 1
        return True

    monkeypatch.setattr(manager, "_compact", compact_without_progress)
    asyncio.run(manager.ensure_context_fits("conv-no-progress"))

    assert calls == 1


def test_context_usage_endpoint_returns_report(monkeypatch):
    llm = RecordingLLM()
    manager = MemoryManager(llm)
    state = manager.get_or_create("conv-123", title="Context route")
    state.summary = "Earlier work summary."
    state.recent_messages = [{"role": "user", "content": "Check the context window"}]
    state.all_messages = list(state.recent_messages)

    registry_tools = [
        {
            "name": "browser_snapshot",
            "description": "Inspect the current browser page.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "file_reader",
            "description": "Compatibility alias for file_read.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            "visible_to_model": False,
        },
    ]

    class FakeRegistry:
        def get_all_tools(self, *, visible_only: bool = False):
            if visible_only:
                return [registry_tools[0]]
            return list(registry_tools)

    fake_runtime = SimpleNamespace(
        llm_client=SimpleNamespace(provider="openai", model_name="gpt-5.6-luna"),
        memory=manager,
        tool_registry=FakeRegistry(),
        skills=[
            SkillSpec(
                slug="core",
                name="core",
                description="Core tools",
                version="1.0.0",
                body="Use the core tools carefully.",
                path=Path("."),
                enabled=True,
            )
        ],
    )

    monkeypatch.setattr(routes_mod, "load_agent_settings", lambda _settings: SimpleNamespace(identity=SimpleNamespace(agent_name="Agent", user_name="", user_identity="", communication_style="")))
    monkeypatch.setattr(routes_mod, "build_runtime_namespace", lambda _base_settings, agent_settings: agent_settings)
    monkeypatch.setattr(routes_mod, "get_runtime", lambda _settings: fake_runtime)
    monkeypatch.setattr(routes_mod, "get_db", lambda: SimpleNamespace(get_conversation=lambda _conv_id: {"id": "conv-123"}))

    client = TestClient(app)
    response = client.get("/api/conversations/conv-123/context-usage")

    assert response.status_code == 200
    payload = response.json()
    assert payload["used"] > 0
    assert payload["limit"] == 256000
    assert payload["compaction_at"] == 236800
    assert payload["estimator"]
    assert any(item["key"] == "messages" for item in payload["breakdown"])
    assert any(item["key"] == "builtin_tools" for item in payload["breakdown"])
    assert any(item["key"] == "deferred_tools" for item in payload["breakdown"])
    assert next(item for item in payload["breakdown"] if item["key"] == "deferred_tools")["kind"] == "excluded"


def test_messages_endpoint_returns_preview_tool_calls_and_detail_endpoint(monkeypatch, tmp_path):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    db.create_conversation("conv-tools", "Tool history")
    user_message_id = db.add_message("conv-tools", "user", "Inspect the page")
    assistant_message_id = db.add_message("conv-tools", "assistant", "Done.")
    assert user_message_id > 0
    long_input = '{"url":"' + ("https://example.com/" + ("a" * 1400)) + '"}'
    long_output = "snapshot:" + ("b" * 1500)
    db.add_tool_call(
        message_id=assistant_message_id,
        conv_id="conv-tools",
        tool_name="browser_snapshot",
        tool_input=long_input,
        tool_output=long_output,
        status="complete",
    )

    monkeypatch.setattr(routes_mod, "get_db", lambda: db)

    client = TestClient(app)
    history = client.get("/api/conversations/conv-tools/messages?tool_call_mode=summary")

    assert history.status_code == 200
    payload = history.json()
    tool_call = payload["messages"][-1]["tool_calls"][0]
    assert tool_call["tool_name"] == "browser_snapshot"
    assert tool_call["preview_only"] is True
    assert tool_call["has_full_input"] is True
    assert tool_call["has_full_output"] is True
    assert tool_call["input"].endswith("[history preview truncated]")
    assert tool_call["output"].endswith("[history preview truncated]")

    detail = client.get(f"/api/messages/{assistant_message_id}/tool-calls")

    assert detail.status_code == 200
    detail_payload = detail.json()
    assert detail_payload[0]["preview_only"] is False
    assert detail_payload[0]["input"] == long_input
    assert detail_payload[0]["output"] == long_output


def test_messages_endpoint_returns_full_tool_calls_by_default(monkeypatch, tmp_path):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    db.create_conversation("conv-tools-default", "Tool history default")
    db.add_message("conv-tools-default", "user", "Inspect the page")
    assistant_message_id = db.add_message("conv-tools-default", "assistant", "Done.")
    long_input = '{"url":"' + ("https://example.com/" + ("a" * 1400)) + '"}'
    long_output = "snapshot:" + ("b" * 1500)
    db.add_tool_call(
        message_id=assistant_message_id,
        conv_id="conv-tools-default",
        tool_name="browser_snapshot",
        tool_input=long_input,
        tool_output=long_output,
        status="complete",
    )

    monkeypatch.setattr(routes_mod, "get_db", lambda: db)

    client = TestClient(app)
    history = client.get("/api/conversations/conv-tools-default/messages")

    assert history.status_code == 200
    payload = history.json()
    tool_call = payload["messages"][-1]["tool_calls"][0]
    assert tool_call["preview_only"] is False
    assert tool_call["has_full_input"] is True
    assert tool_call["has_full_output"] is True
    assert tool_call["input"] == long_input
    assert tool_call["output"] == long_output


def test_messages_endpoint_returns_stable_pagination_cursor(monkeypatch, tmp_path):
    db = Database(tmp_path / "agent.db")
    db.init_db()
    db.create_conversation("conv-pages", "Paged history")
    message_ids = [
        db.add_message("conv-pages", "user" if index % 2 == 0 else "assistant", f"message {index}")
        for index in range(8)
    ]

    monkeypatch.setattr(routes_mod, "get_db", lambda: db)

    client = TestClient(app)
    first = client.get("/api/conversations/conv-pages/messages?limit=3&tool_call_mode=none")

    assert first.status_code == 200
    first_payload = first.json()
    assert [message["id"] for message in first_payload["messages"]] == message_ids[-3:]
    assert first_payload["has_more"] is True
    assert first_payload["next_before_id"] == message_ids[-3]

    second = client.get(
        f"/api/conversations/conv-pages/messages?limit=3&before_id={first_payload['next_before_id']}&tool_call_mode=none"
    )

    assert second.status_code == 200
    second_payload = second.json()
    assert [message["id"] for message in second_payload["messages"]] == message_ids[-6:-3]
    assert second_payload["has_more"] is True
    assert second_payload["next_before_id"] == message_ids[-6]

    final = client.get(
        f"/api/conversations/conv-pages/messages?limit=3&before_id={second_payload['next_before_id']}&tool_call_mode=none"
    )

    assert final.status_code == 200
    final_payload = final.json()
    assert [message["id"] for message in final_payload["messages"]] == message_ids[:2]
    assert final_payload["has_more"] is False
    assert final_payload["next_before_id"] == message_ids[0]


def test_context_usage_report_counts_runtime_skills_and_tools():
    llm = RecordingLLM()
    manager = MemoryManager(llm)
    state = manager.get_or_create("conv-breakdown", title="Breakdown")
    state.summary = "Summarized earlier work."
    state.recent_messages = [
        {"role": "user", "content": "Open the page"},
        {"role": "assistant", "content": "Inspecting the page now."},
    ]
    state.all_messages = list(state.recent_messages)

    report = build_context_usage_report(
        memory=manager,
        llm_client=SimpleNamespace(provider="openai", model_name="gpt-5.6-luna"),
        conversation_id="conv-breakdown",
        runtime_prompt_text="Runtime instructions go here.",
        skill_prompt_text="Skill guidance goes here.",
        long_term_context="Long-term memory snippet.",
        visible_tools=[
            {
                "name": "browser_snapshot",
                "description": "Inspect the current page.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "mcp__filesystem__list_directory",
                "description": "List directory contents.",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                "mcp_bridge": {"server_name": "filesystem"},
            },
        ],
        hidden_tools=[
            {
                "name": "file_reader",
                "description": "Compatibility alias for file_read.",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            }
        ],
        limit=150000,
        compaction_at=135000,
    )

    assert report["used"] > 0
    assert report["free_tokens"] == 150000 - report["used"]
    assert report["compaction_buffer_tokens"] == max(135000 - report["used"], 0)
    assert any(item["key"] == "runtime_prompt" for item in report["breakdown"])
    assert any(item["key"] == "skills" for item in report["breakdown"])
    assert any(item["key"] == "memory_retrieval" for item in report["breakdown"])
    assert any(item["key"] == "builtin_tools" for item in report["breakdown"])
    assert any(item["key"] == "mcp_tools" for item in report["breakdown"])
    assert any(item["key"] == "deferred_tools" for item in report["breakdown"])
    assert report["used"] == sum(item["tokens"] for item in report["breakdown"] if item["kind"] == "used")


def test_context_usage_uses_model_specific_window_and_tokenizer():
    luna = SimpleNamespace(provider="openai", model_name="gpt-5.6-luna")
    luna_snapshot = SimpleNamespace(provider="openai", model_name="gpt-5.6-luna-2026-03-17")
    gemini = SimpleNamespace(provider="gemini", model_name="gemini-3.1-pro-preview")
    unknown = SimpleNamespace(provider="openai", model_name="gpt-unlisted")
    gpt6 = SimpleNamespace(provider="openai", model_name="gpt-6-luna")

    assert model_context_token_limit(luna) == 256_000
    assert model_compaction_threshold(luna) == 236_800
    assert model_context_token_limit(luna_snapshot) == 256_000
    assert model_context_token_limit(gemini) == 256_000
    assert model_compaction_threshold(gemini) == 236_800
    assert model_context_token_limit(gpt6) == 256_000
    assert model_compaction_threshold(gpt6) == 236_800
    assert model_context_token_limit(unknown) == 200_000
    assert token_estimation_method(luna) in {"character estimate (no tokenizer)", "tiktoken:o200k_base estimate", "tiktoken:cl100k_base estimate"}
    assert token_estimation_method(gemini) == "Gemini character estimate (not exact tokenizer)"
    assert count_text_tokens("你好世界", llm_client=gemini) >= 4


def test_openai_context_estimate_includes_responses_tool_history():
    client = SimpleNamespace(provider="openai", model_name="gpt-6-luna", supports_vision=False)
    plain = estimate_message_tokens([{"role": "user", "content": "Use a tool"}], llm_client=client)
    with_tool_history = estimate_message_tokens([
        {"role": "user", "content": "Use a tool"},
        {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": '{"query":"hello"}'},
        {"type": "function_call_output", "call_id": "call_1", "output": "Found a useful result"},
    ], llm_client=client)

    assert with_tool_history > plain + count_text_tokens("Found a useful result", llm_client=client)
