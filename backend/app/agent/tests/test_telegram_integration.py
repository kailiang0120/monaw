import asyncio
import json
import re
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest

from app.agent.llm_constants import DEEPSEEK_CHAT_MODELS, OPENAI_CHAT_MODELS
from app.agent.response_attachments import collect_response_attachments, resolve_attachment_path
from app.agent.speech_to_text import (
    VOICE_MODEL_NOT_READY_MESSAGE,
    SpeechToTextCloudNotConfigured,
    SpeechToTextModelMissing,
)
from app.integrations.telegram import agent_bridge as telegram_agent_bridge
from app.integrations.telegram import bridge as telegram_bridge
from app.integrations.telegram.agent_bridge import (
    TelegramAgentBridge,
    TelegramEffortSummary,
    TelegramModelSummary,
    TelegramTurnResult,
    simplify_telegram_text,
    split_telegram_message,
)
from app.integrations.telegram.bridge import (
    COMMANDS,
    TELEGRAM_AUTH_CONFIG_KEY,
    TELEGRAM_PERMISSION_CALLBACK_PREFIX,
    TelegramAuthorization,
    _ensure_authorized,
    _format_effort_options,
    _format_model_options,
    _format_resume_options,
    _help_message,
    _parse_id_set,
    _permission_notice,
    _download_telegram_file,
    _handle_authorized_message,
    permission_callback,
    prepare_telegram_message_input,
    register_bot_commands,
    send_response_attachments,
)
from app.integrations.telegram.session import TelegramSessionStore, default_conversation_id


def _workspace_tmp_dir(name: str) -> Path:
    path = Path.cwd() / f".tmp-{name}-{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def test_telegram_conversation_id_is_stable_and_valid():
    conv_id = default_conversation_id("-1001234567890", thread_id=42)

    assert conv_id == default_conversation_id("-1001234567890", thread_id=42)
    assert len(conv_id) <= 64
    assert re.fullmatch(r"[A-Za-z0-9_-]+", conv_id)
    assert conv_id.startswith("telegram_")


def test_telegram_session_store_persists_current_and_new_session():
    tmp_dir = _workspace_tmp_dir("telegram-session-store")
    try:
        store = TelegramSessionStore(tmp_dir / "sessions.json")

        first = store.get_current(12345)
        assert first == store.get_current(12345)

        second = store.start_new(12345)
        assert second != first
        assert store.get_current(12345) == second

        reloaded = TelegramSessionStore(tmp_dir / "sessions.json")
        assert reloaded.get_current(12345) == second

        reloaded.set_current(12345, first)
        assert reloaded.get_current(12345) == first
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_telegram_session_store_persists_model_selection():
    tmp_dir = _workspace_tmp_dir("telegram-model-store")
    try:
        store = TelegramSessionStore(tmp_dir / "sessions.json")

        selection = store.set_model_selection(
            "conv-alpha",
            provider="openai",
            model_name="gpt-5.4-mini",
        )

        assert selection == {"provider": "openai", "model_name": "gpt-5.4-mini"}
        assert store.get_model_selection("conv-alpha") == selection

        reloaded = TelegramSessionStore(tmp_dir / "sessions.json")
        assert reloaded.get_model_selection("conv-alpha") == selection
        assert reloaded.clear_model_selection("conv-alpha") is True
        assert reloaded.get_model_selection("conv-alpha") is None
        assert reloaded.clear_model_selection("conv-alpha") is False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_telegram_session_store_persists_effort_selection():
    tmp_dir = _workspace_tmp_dir("telegram-effort-store")
    try:
        store = TelegramSessionStore(tmp_dir / "sessions.json")

        assert store.set_effort_selection("conv-alpha", "high") == "high"
        assert store.get_effort_selection("conv-alpha") == "high"

        reloaded = TelegramSessionStore(tmp_dir / "sessions.json")
        assert reloaded.get_effort_selection("conv-alpha") == "high"
        assert reloaded.clear_effort_selection("conv-alpha") is True
        assert reloaded.get_effort_selection("conv-alpha") == ""
        assert reloaded.clear_effort_selection("conv-alpha") is False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_telegram_authorization_parses_and_matches_user_or_chat_ids():
    authorization = TelegramAuthorization(
        allowed_user_ids=_parse_id_set("101, 202", setting_name="TEST_USER_IDS"),
        allowed_chat_ids=_parse_id_set("-100303", setting_name="TEST_CHAT_IDS"),
    )
    user_allowed = SimpleNamespace(
        effective_user=SimpleNamespace(id=202),
        effective_chat=SimpleNamespace(id=999),
    )
    chat_allowed = SimpleNamespace(
        effective_user=SimpleNamespace(id=404),
        effective_chat=SimpleNamespace(id=-100303),
    )
    rejected = SimpleNamespace(
        effective_user=SimpleNamespace(id=404),
        effective_chat=SimpleNamespace(id=505),
    )

    assert authorization.is_authorized(user_allowed) is True
    assert authorization.is_authorized(chat_allowed) is True
    assert authorization.is_authorized(rejected) is False


def test_telegram_authorization_rejects_invalid_config_ids():
    with pytest.raises(RuntimeError, match="non-integer"):
        _parse_id_set("123, abc", setting_name="TEST_USER_IDS")


def test_telegram_bot_requires_allowlist_by_default(monkeypatch):
    monkeypatch.setattr(telegram_bridge.settings, "telegram_allowed_user_ids", "")
    monkeypatch.setattr(telegram_bridge.settings, "telegram_allowed_chat_ids", "")
    monkeypatch.setattr(telegram_bridge.settings, "telegram_allow_all", False)

    with pytest.raises(RuntimeError, match="TELEGRAM_ALLOWED_USER_IDS"):
        telegram_bridge.build_application("123:abc")


def test_ensure_authorized_sends_rejection_for_unknown_chat():
    calls = []

    class Bot:
        async def send_message(self, **kwargs):
            calls.append(kwargs)

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=111),
        effective_chat=SimpleNamespace(id=222),
    )
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                TELEGRAM_AUTH_CONFIG_KEY: TelegramAuthorization(
                    allowed_user_ids=frozenset({999}),
                    allowed_chat_ids=frozenset(),
                )
            }
        ),
        bot=Bot(),
    )

    assert asyncio.run(_ensure_authorized(update, context)) is False
    assert calls == [
        {
            "chat_id": 222,
            "text": "This Telegram chat is not authorized for this agent.",
        }
    ]


def test_split_telegram_message_respects_safe_chunk_size():
    chunks = split_telegram_message("a" * 9000, max_length=3800)

    assert len(chunks) == 3
    assert all(0 < len(chunk) <= 3800 for chunk in chunks)
    assert "".join(chunks) == "a" * 9000


def test_simplify_telegram_text_removes_markdown_formatting():
    text = simplify_telegram_text(
        """
        ## Summary

        **Status:** Done
        - See [report](C:/tmp/report.md)
        > _Note_: ready

        ```python
        print("ok")
        ```
        """
    )

    assert text == "\n".join(
        [
            "Summary",
            "",
            "Status: Done",
            "- See report",
            "Note: ready",
            "",
            "print(\"ok\")",
        ]
    )
    assert "**" not in text
    assert "```" not in text
    assert "](" not in text


def test_telegram_agent_bridge_returns_simplified_reply_but_keeps_attachment_detection():
    tmp_dir = _workspace_tmp_dir("telegram-plain-reply")
    try:
        output_path = tmp_dir / "report.md"
        output_path.write_text("result", encoding="utf-8")

        async def fake_runner(**_kwargs):
            yield {
                "event": "token",
                "data": {"content": f"## Done\n\nSaved **report** at `{output_path}`"},
            }
            yield {"event": "done", "data": {"summary": "Done", "status": "complete"}}

        bridge = TelegramAgentBridge(
            session_store=TelegramSessionStore(tmp_dir / "sessions.json"),
            settings_factory=lambda: object(),
            runner=fake_runner,
            initialize_conversation=lambda _conv_id, _title: None,
        )

        result = asyncio.run(bridge.run_chat_message(chat_id=12345, text="Create report"))

        assert result.reply == f"Done\n\nSaved report at {output_path}"
        assert result.attachments[0]["name"] == "report.md"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_telegram_agent_bridge_reuses_chat_session():
    tmp_dir = _workspace_tmp_dir("telegram-agent-bridge")
    try:
        calls = []
        output_path = tmp_dir / "output.txt"
        output_path.write_text("result", encoding="utf-8")

        async def fake_runner(**kwargs):
            calls.append(kwargs)
            yield {"event": "token", "data": {"content": "Reply"}}
            yield {
                "event": "done",
                "data": {
                    "summary": "Reply",
                    "status": "complete",
                    "attachments": collect_response_attachments(content=f"File: {output_path}"),
                },
            }

        bridge = TelegramAgentBridge(
            session_store=TelegramSessionStore(tmp_dir / "sessions.json"),
            settings_factory=lambda: object(),
            runner=fake_runner,
            initialize_conversation=lambda _conv_id, _title: None,
        )

        first = asyncio.run(bridge.run_chat_message(chat_id=12345, text="Hello"))
        second = asyncio.run(bridge.run_chat_message(chat_id=12345, text="Continue"))

        assert first.reply == "Reply"
        assert first.attachments[0]["name"] == "output.txt"
        assert second.reply == "Reply"
        assert first.conversation_id == second.conversation_id
        assert [call["conversation_id"] for call in calls] == [first.conversation_id, first.conversation_id]
        assert [call["message"] for call in calls] == ["Hello", "Continue"]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_telegram_agent_bridge_passes_input_attachments_to_runner():
    tmp_dir = _workspace_tmp_dir("telegram-input-attachments")
    try:
        calls = []
        attachment = {
            "id": "upload-1",
            "name": "photo.jpg",
            "path": str(tmp_dir / "photo.jpg"),
            "mime_type": "image/jpeg",
            "size": 12,
        }

        async def fake_runner(**kwargs):
            calls.append(kwargs)
            yield {"event": "token", "data": {"content": "Reply"}}
            yield {"event": "done", "data": {"summary": "Reply", "status": "complete"}}

        bridge = TelegramAgentBridge(
            session_store=TelegramSessionStore(tmp_dir / "sessions.json"),
            settings_factory=lambda: object(),
            runner=fake_runner,
            initialize_conversation=lambda _conv_id, _title: None,
        )

        result = asyncio.run(
            bridge.run_chat_message(
                chat_id=12345,
                text="Please inspect this image",
                attachments=[attachment],
            )
        )

        assert result.reply == "Reply"
        assert calls[0]["attachments"] == [attachment]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_telegram_bridge_sets_model_for_chat_runtime():
    tmp_dir = _workspace_tmp_dir("telegram-model-bridge")
    try:
        calls = []

        async def fake_runner(**kwargs):
            calls.append(kwargs)
            yield {"event": "token", "data": {"content": "Reply"}}
            yield {"event": "done", "data": {"summary": "Reply", "status": "complete"}}

        store = TelegramSessionStore(tmp_dir / "sessions.json")
        base_settings = SimpleNamespace(
            model_provider="openai",
            model_name="gpt-5.4",
            reasoning_effort="medium",
            llm=SimpleNamespace(
                provider="openai",
                model_name="gpt-5.4",
                reasoning_effort="medium",
            ),
            browser=SimpleNamespace(allowed_domains=["localhost"]),
        )
        bridge = TelegramAgentBridge(
            session_store=store,
            settings_factory=lambda: base_settings,
            runner=fake_runner,
            initialize_conversation=lambda _conv_id, _title: None,
        )

        model, error = bridge.set_model_selection(12345, "gpt-5.4-mini")
        result = asyncio.run(bridge.run_chat_message(chat_id=12345, text="Hello"))

        assert error == ""
        assert model is not None
        assert model.provider == "openai"
        assert model.model_name == "gpt-5.4-mini"
        assert result.reply == "Reply"
        runtime_settings = calls[0]["settings"]
        assert runtime_settings.model_provider == "openai"
        assert runtime_settings.model_name == "gpt-5.4-mini"
        assert runtime_settings.llm.provider == "openai"
        assert runtime_settings.llm.model_name == "gpt-5.4-mini"
        assert runtime_settings.browser is not base_settings.browser
        assert base_settings.model_name == "gpt-5.4"
        assert base_settings.llm.model_name == "gpt-5.4"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_telegram_bridge_lists_models_and_selects_by_number_or_name():
    tmp_dir = _workspace_tmp_dir("telegram-model-list")
    try:
        store = TelegramSessionStore(tmp_dir / "sessions.json")
        bridge = TelegramAgentBridge(
            session_store=store,
            settings_factory=lambda: SimpleNamespace(
                model_provider="openai",
                model_name=OPENAI_CHAT_MODELS[0],
                reasoning_effort="medium",
            ),
            runner=lambda **_kwargs: None,
            initialize_conversation=lambda _conv_id, _title: None,
        )

        options = bridge.list_model_options(12345)
        first_three = [
            (option.index, option.provider, option.model_name, option.is_current)
            for option in options[:3]
        ]
        assert first_three == [
            (1, "openai", OPENAI_CHAT_MODELS[0], True),
            (2, "openai", OPENAI_CHAT_MODELS[1], False),
            (3, "openai", OPENAI_CHAT_MODELS[2], False),
        ]

        selected, error = bridge.set_model_selection(12345, "2")
        assert error == ""
        assert selected is not None
        assert selected.model_name == OPENAI_CHAT_MODELS[1]

        selected, error = bridge.set_model_selection(12345, f"deepseek:{DEEPSEEK_CHAT_MODELS[0]}")
        assert error == ""
        assert selected is not None
        assert selected.provider == "deepseek"
        assert selected.model_name == DEEPSEEK_CHAT_MODELS[0]

        current = bridge.current_model_summary(12345)
        assert current.is_override is True
        assert current.model_name == DEEPSEEK_CHAT_MODELS[0]

        default = bridge.clear_model_selection(12345)
        assert default.is_override is False
        assert default.provider == "openai"
        assert default.model_name == OPENAI_CHAT_MODELS[0]
        assert store.get_model_selection(store.get_current(12345)) is None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_telegram_bridge_sets_and_clears_reasoning_effort():
    tmp_dir = _workspace_tmp_dir("telegram-effort-bridge")
    try:
        calls = []
        base_settings = SimpleNamespace(
            model_provider="openai",
            model_name=OPENAI_CHAT_MODELS[0],
            reasoning_effort="medium",
            llm=SimpleNamespace(
                provider="openai",
                model_name=OPENAI_CHAT_MODELS[0],
                reasoning_effort="medium",
            ),
        )

        async def fake_runner(**kwargs):
            calls.append(kwargs)
            yield {"event": "token", "data": {"content": "Reply"}}
            yield {"event": "done", "data": {"summary": "Reply", "status": "complete"}}

        store = TelegramSessionStore(tmp_dir / "sessions.json")
        bridge = TelegramAgentBridge(
            session_store=store,
            settings_factory=lambda: base_settings,
            runner=fake_runner,
            initialize_conversation=lambda _conv_id, _title: None,
        )

        options = bridge.list_effort_options(12345)
        assert [(option.index, option.effort, option.is_current) for option in options[:4]] == [
            (1, "none", False),
            (2, "minimal", False),
            (3, "low", False),
            (4, "medium", True),
        ]

        selected, error = bridge.set_effort_selection(12345, "high")
        assert error == ""
        assert selected is not None
        assert selected.effort == "high"
        assert selected.is_override is True

        result = asyncio.run(bridge.run_chat_message(chat_id=12345, text="Hello"))
        assert result.reply == "Reply"
        runtime_settings = calls[0]["settings"]
        assert runtime_settings.reasoning_effort == "high"
        assert runtime_settings.llm.reasoning_effort == "high"
        assert base_settings.reasoning_effort == "medium"

        cleared = bridge.clear_effort_selection(12345)
        assert cleared.effort == "medium"
        assert cleared.is_override is False
        assert store.get_effort_selection(store.get_current(12345)) == ""
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_telegram_bridge_lists_and_resumes_by_chat_name():
    tmp_dir = _workspace_tmp_dir("telegram-resume")
    try:
        conversations = [
            {"id": "conv-beta", "title": "Beta project"},
            {"id": "conv-alpha", "title": "Alpha planning"},
        ]
        by_id = {conversation["id"]: conversation for conversation in conversations}
        store = TelegramSessionStore(tmp_dir / "sessions.json")
        store.set_current(12345, "conv-alpha")
        bridge = TelegramAgentBridge(
            session_store=store,
            settings_factory=lambda: object(),
            runner=lambda **_kwargs: None,
            initialize_conversation=lambda _conv_id, _title: None,
            get_conversation=lambda conv_id: by_id.get(conv_id),
            list_conversations=lambda: conversations,
        )

        options = bridge.list_resume_options(12345)
        assert [(option.index, option.title, option.is_current) for option in options] == [
            (1, "Beta project", False),
            (2, "Alpha planning", True),
        ]

        resumed, error = bridge.resume_session(12345, "Beta project")
        assert error == ""
        assert resumed is not None
        assert resumed.title == "Beta project"
        assert store.get_current(12345) == "conv-beta"

        resumed, error = bridge.resume_session(12345, "2")
        assert error == ""
        assert resumed is not None
        assert resumed.title == "Alpha planning"
        assert store.get_current(12345) == "conv-alpha"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_telegram_bridge_renames_current_session():
    tmp_dir = _workspace_tmp_dir("telegram-rename")
    try:
        conversations = {}
        store = TelegramSessionStore(tmp_dir / "sessions.json")

        def initialize(conversation_id, title):
            conversations.setdefault(conversation_id, {"id": conversation_id, "title": title})

        bridge = TelegramAgentBridge(
            session_store=store,
            settings_factory=lambda: object(),
            runner=lambda **_kwargs: None,
            initialize_conversation=initialize,
            get_conversation=lambda conv_id: conversations.get(conv_id),
        )

        calls = []
        original_get_db = telegram_agent_bridge.get_db
        telegram_agent_bridge.get_db = lambda: SimpleNamespace(
            update_conversation=lambda conv_id, **fields: calls.append((conv_id, fields))
        )
        try:
            renamed, error = bridge.rename_current_session(12345, "  Project Alpha  ")
        finally:
            telegram_agent_bridge.get_db = original_get_db

        assert error == ""
        assert renamed is not None
        assert renamed.title == "Project Alpha"
        assert calls == [(store.get_current(12345), {"title": "Project Alpha"})]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_format_resume_options_uses_chat_names_only():
    class Option:
        def __init__(self, index, title, is_current=False):
            self.index = index
            self.title = title
            self.is_current = is_current

    text = _format_resume_options(
        [
            Option(1, "Beta project"),
            Option(2, "Alpha planning", is_current=True),
        ]
    )

    assert "Beta project" in text
    assert "Alpha planning (current)" in text
    assert "conv-" not in text


def test_format_model_options_uses_model_names_only():
    current = TelegramModelSummary(
        provider="openai",
        model_name="gpt-5.4-mini",
        is_current=True,
        is_override=True,
    )
    text = _format_model_options(
        [
            TelegramModelSummary(index=1, provider="openai", model_name="gpt-5.4"),
            TelegramModelSummary(
                index=2,
                provider="openai",
                model_name="gpt-5.4-mini",
                is_current=True,
            ),
        ],
        current,
    )

    assert "Current model: OpenAI - gpt-5.4-mini" in text
    assert "2. OpenAI - gpt-5.4-mini (current)" in text
    assert "conv-" not in text
    assert "session" not in text.casefold()


def test_format_effort_options_uses_effort_names_only():
    current = TelegramEffortSummary(
        effort="high",
        is_current=True,
        is_override=True,
    )
    text = _format_effort_options(
        [
            TelegramEffortSummary(index=1, effort="medium"),
            TelegramEffortSummary(index=2, effort="high", is_current=True),
        ],
        current,
    )

    assert "Current effort: High" in text
    assert "2. High (current)" in text
    assert "desktop app default" not in text.casefold()


def test_register_bot_commands_sets_expected_shortcuts():
    calls = []

    async def fake_set_my_commands(commands):
        calls.append(commands)

    bot = SimpleNamespace(set_my_commands=fake_set_my_commands)
    asyncio.run(register_bot_commands(bot))

    assert calls == [COMMANDS]


def test_help_message_lists_all_registered_commands():
    text = _help_message()

    for command in COMMANDS:
        assert f"/{command.command}" in text


def test_telegram_permission_notice_uses_inline_buttons():
    text, keyboard = _permission_notice({
        "event": "approval_required",
        "data": {
            "ticket_id": "ticket-1",
            "action": "Delete file",
            "reason": "High-risk action",
        },
    })

    assert "Approval required" in text
    assert "ticket-1" in text
    assert keyboard.inline_keyboard[0][0].callback_data == (
        f"{TELEGRAM_PERMISSION_CALLBACK_PREFIX}:approve:ticket-1"
    )
    assert keyboard.inline_keyboard[0][1].callback_data == (
        f"{TELEGRAM_PERMISSION_CALLBACK_PREFIX}:reject:ticket-1"
    )


def test_telegram_permission_callback_resumes_approval(monkeypatch):
    calls = []

    class Query:
        data = f"{TELEGRAM_PERMISSION_CALLBACK_PREFIX}:approve:ticket-1"

        async def answer(self, text):
            calls.append(("answer", text))

        async def edit_message_text(self, text):
            calls.append(("edit", text))

    monkeypatch.setattr(
        telegram_bridge,
        "approve_ticket",
        lambda ticket_id, resolved_by: SimpleNamespace(id=ticket_id, resolved_by=resolved_by),
    )
    monkeypatch.setattr(
        telegram_bridge,
        "signal_approval_resume",
        lambda ticket_id, decision: calls.append(("signal", ticket_id, decision)),
    )

    update = SimpleNamespace(
        callback_query=Query(),
        effective_chat=SimpleNamespace(id=222),
        effective_user=SimpleNamespace(id=111),
    )
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                TELEGRAM_AUTH_CONFIG_KEY: TelegramAuthorization(
                    allowed_user_ids=frozenset({111}),
                    allowed_chat_ids=frozenset(),
                )
            }
        ),
        bot=SimpleNamespace(send_message=lambda **_kwargs: None),
    )

    asyncio.run(permission_callback(update, context))

    assert ("signal", "ticket-1", "approved") in calls
    assert ("answer", "Approved.") in calls
    assert any(call[0] == "edit" and "Approved ticket ticket-1" in call[1] for call in calls)


def test_telegram_permission_callback_resumes_access_grant(monkeypatch):
    calls = []

    class Query:
        data = f"{TELEGRAM_PERMISSION_CALLBACK_PREFIX}:grant_session:grant-1"

        async def answer(self, text):
            calls.append(("answer", text))

        async def edit_message_text(self, text):
            calls.append(("edit", text))

    monkeypatch.setattr(
        telegram_bridge,
        "resolve_grant",
        lambda ticket_id, decision: SimpleNamespace(id=ticket_id, decision=decision),
    )
    monkeypatch.setattr(
        telegram_bridge,
        "signal_grant_resume",
        lambda ticket_id, decision: calls.append(("signal", ticket_id, decision)),
    )

    update = SimpleNamespace(
        callback_query=Query(),
        effective_chat=SimpleNamespace(id=222),
        effective_user=SimpleNamespace(id=111),
    )
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                TELEGRAM_AUTH_CONFIG_KEY: TelegramAuthorization(
                    allowed_user_ids=frozenset({111}),
                    allowed_chat_ids=frozenset(),
                )
            }
        ),
        bot=SimpleNamespace(send_message=lambda **_kwargs: None),
    )

    asyncio.run(permission_callback(update, context))

    assert ("signal", "grant-1", "session") in calls
    assert ("answer", "Granted session") in calls
    assert any(call[0] == "edit" and "Granted session for ticket grant-1" in call[1] for call in calls)


def test_collect_response_attachments_from_tool_output():
    tmp_dir = _workspace_tmp_dir("telegram-attachments")
    try:
        image_path = tmp_dir / "result image.png"
        doc_path = tmp_dir / "report.pdf"
        exe_path = tmp_dir / "cmd.exe"
        image_path.write_bytes(b"png")
        doc_path.write_bytes(b"pdf")
        exe_path.write_bytes(b"exe")

        attachments = collect_response_attachments(
            content=f"Saved report at {doc_path}",
            tool_calls=[
                {
                    "output": json.dumps(
                        {
                            "status": "ok",
                            "screenshot_path": str(image_path),
                            "path": str(doc_path),
                            "process": {"exe": str(exe_path), "name": "cmd.exe"},
                        }
                    )
                }
            ],
        )

        assert [attachment["name"] for attachment in attachments] == ["report.pdf", "result image.png"]
        assert attachments[0]["mime_type"] == "application/pdf"
        assert attachments[1]["mime_type"] == "image/png"
        assert resolve_attachment_path(attachments[0]["id"]) == doc_path.resolve(strict=False)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_send_response_attachments_falls_back_to_document_for_invalid_photo_dimensions():
    tmp_dir = _workspace_tmp_dir("telegram-photo-fallback")
    try:
        image_path = tmp_dir / "wide.png"
        image_path.write_bytes(b"png")

        class Bot:
            def __init__(self):
                self.calls = []

            async def send_photo(self, **kwargs):
                self.calls.append(("photo", kwargs["chat_id"], kwargs["caption"]))
                raise BadRequest("Photo_invalid_dimensions")

            async def send_document(self, **kwargs):
                self.calls.append(("document", kwargs["chat_id"], kwargs["filename"], kwargs["caption"]))

            async def send_message(self, **kwargs):
                self.calls.append(("message", kwargs["chat_id"], kwargs["text"]))

        bot = Bot()
        asyncio.run(send_response_attachments(
            bot,
            12345,
            [{"path": str(image_path), "mime_type": "image/png", "size": image_path.stat().st_size}],
        ))

        assert bot.calls == [
            ("photo", 12345, "wide.png"),
            ("document", 12345, "wide.png", "wide.png"),
        ]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_prepare_telegram_message_input_downloads_photo_and_document(monkeypatch):
    tmp_dir = _workspace_tmp_dir("telegram-input-download")
    try:
        class TelegramFile:
            def __init__(self, data: bytes):
                self.data = data

            async def download_to_drive(self, custom_path: str):
                Path(custom_path).write_bytes(self.data)

        class Bot:
            async def get_file(self, file_id: str):
                return TelegramFile({"photo-large": b"jpg", "doc-1": b"pdf"}[file_id])

        monkeypatch.setattr(telegram_bridge, "runtime_path", lambda *parts: tmp_dir.joinpath(*parts))
        monkeypatch.setattr(telegram_bridge, "register_attachment_path", lambda _id, _path: None)

        message = SimpleNamespace(
            text="",
            caption="Please inspect these",
            photo=[
                SimpleNamespace(file_id="photo-small", file_unique_id="small", file_size=1, width=10, height=10),
                SimpleNamespace(file_id="photo-large", file_unique_id="large", file_size=3, width=100, height=100),
            ],
            document=SimpleNamespace(
                file_id="doc-1",
                file_name="report.pdf",
                mime_type="application/pdf",
                file_size=3,
            ),
            voice=None,
            audio=None,
        )
        context = SimpleNamespace(bot=Bot())
        update = SimpleNamespace(effective_message=message)

        prepared = asyncio.run(
            prepare_telegram_message_input(update, context, conversation_id="telegram_test")
        )

        assert prepared.text.startswith("Please inspect these")
        assert "Uploaded file(s):" in prepared.text
        assert [attachment["mime_type"] for attachment in prepared.attachments] == [
            "image/jpeg",
            "application/pdf",
        ]
        assert all(Path(attachment["path"]).is_file() for attachment in prepared.attachments)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_telegram_file_download_timeout_returns_clear_error(monkeypatch):
    tmp_dir = _workspace_tmp_dir("telegram-download-timeout")
    try:
        class Bot:
            async def get_file(self, _file_id: str):
                await asyncio.sleep(0.05)

        monkeypatch.setattr(telegram_bridge, "TELEGRAM_FILE_DOWNLOAD_TIMEOUT_SECONDS", 0.01)

        with pytest.raises(TimeoutError, match="download timed out"):
            asyncio.run(_download_telegram_file(Bot(), "file-1", tmp_dir / "file.txt"))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_prepare_telegram_message_input_transcribes_voice(monkeypatch):
    tmp_dir = _workspace_tmp_dir("telegram-voice-input")
    try:
        class TelegramFile:
            async def download_to_drive(self, custom_path: str):
                Path(custom_path).write_bytes(b"voice")

        class Bot:
            async def get_file(self, _file_id: str):
                return TelegramFile()

        async def fake_transcribe(path):
            assert Path(path).read_bytes() == b"voice"
            return "hello from voice"

        monkeypatch.setattr(telegram_bridge, "runtime_path", lambda *parts: tmp_dir.joinpath(*parts))
        monkeypatch.setattr(telegram_bridge, "transcribe_audio_file", fake_transcribe)

        message = SimpleNamespace(
            text="",
            caption="",
            photo=[],
            document=None,
            voice=SimpleNamespace(
                file_id="voice-1",
                file_unique_id="voice-unique",
                mime_type="audio/ogg",
                file_size=5,
            ),
            audio=None,
        )
        context = SimpleNamespace(bot=Bot())
        update = SimpleNamespace(effective_message=message)

        prepared = asyncio.run(
            prepare_telegram_message_input(update, context, conversation_id="telegram_test")
        )

        assert prepared.text == "Voice transcript:\nhello from voice"
        assert prepared.attachments == ()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_telegram_voice_missing_model_returns_clear_message(monkeypatch):
    tmp_dir = _workspace_tmp_dir("telegram-missing-voice-model")
    try:
        calls = []

        class Bot:
            async def send_chat_action(self, **kwargs):
                calls.append(("action", kwargs))

            async def send_message(self, **kwargs):
                calls.append(("message", kwargs))

        async def fake_prepare(*_args, **_kwargs):
            raise SpeechToTextModelMissing(VOICE_MODEL_NOT_READY_MESSAGE)

        bridge = TelegramAgentBridge(
            session_store=TelegramSessionStore(tmp_dir / "sessions.json"),
            runner=lambda **_kwargs: None,
            initialize_conversation=lambda _conv_id, _title: None,
        )
        context = SimpleNamespace(
            bot=Bot(),
            application=SimpleNamespace(bot_data={"agent_bridge": bridge}),
        )
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=12345),
            effective_message=SimpleNamespace(message_thread_id=None),
            effective_user=SimpleNamespace(full_name="Kai", username="kai", id=99),
        )

        monkeypatch.setattr(telegram_bridge, "prepare_telegram_message_input", fake_prepare)

        asyncio.run(_handle_authorized_message(update, context))

        assert ("message", {"chat_id": 12345, "text": VOICE_MODEL_NOT_READY_MESSAGE}) in calls
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_telegram_voice_cloud_missing_key_returns_clear_message(monkeypatch):
    tmp_dir = _workspace_tmp_dir("telegram-missing-cloud-stt-key")
    try:
        calls = []

        class Bot:
            async def send_chat_action(self, **kwargs):
                calls.append(("action", kwargs))

            async def send_message(self, **kwargs):
                calls.append(("message", kwargs))

        async def fake_prepare(*_args, **_kwargs):
            raise SpeechToTextCloudNotConfigured("Google API key is required for cloud speech-to-text.")

        bridge = TelegramAgentBridge(
            session_store=TelegramSessionStore(tmp_dir / "sessions.json"),
            runner=lambda **_kwargs: None,
            initialize_conversation=lambda _conv_id, _title: None,
        )
        context = SimpleNamespace(
            bot=Bot(),
            application=SimpleNamespace(bot_data={"agent_bridge": bridge}),
        )
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=12345),
            effective_message=SimpleNamespace(message_thread_id=None),
            effective_user=SimpleNamespace(full_name="Kai", username="kai", id=99),
        )

        monkeypatch.setattr(telegram_bridge, "prepare_telegram_message_input", fake_prepare)

        asyncio.run(_handle_authorized_message(update, context))

        assert ("message", {"chat_id": 12345, "text": "Google API key is required for cloud speech-to-text."}) in calls
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_telegram_handler_sends_failure_when_agent_run_raises(monkeypatch):
    tmp_dir = _workspace_tmp_dir("telegram-agent-run-failure")
    try:
        calls = []

        class Bot:
            async def send_chat_action(self, **kwargs):
                calls.append(("action", kwargs))

            async def send_message(self, **kwargs):
                calls.append(("message", kwargs))

        class FailingBridge(TelegramAgentBridge):
            async def run_chat_message(self, **_kwargs):
                raise RuntimeError("runner exploded")

        async def fake_prepare(*_args, **_kwargs):
            return telegram_bridge.TelegramPreparedInput(text="hello")

        bridge = FailingBridge(
            session_store=TelegramSessionStore(tmp_dir / "sessions.json"),
            runner=lambda **_kwargs: None,
            initialize_conversation=lambda _conv_id, _title: None,
        )
        context = SimpleNamespace(
            bot=Bot(),
            application=SimpleNamespace(bot_data={"agent_bridge": bridge}),
        )
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=12345),
            effective_message=SimpleNamespace(message_thread_id=None),
            effective_user=SimpleNamespace(full_name="Kai", username="kai", id=99),
        )

        monkeypatch.setattr(telegram_bridge, "prepare_telegram_message_input", fake_prepare)

        asyncio.run(_handle_authorized_message(update, context))

        assert (
            "message",
            {
                "chat_id": 12345,
                "text": "Telegram request failed before I could produce a response: runner exploded",
            },
        ) in calls
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_telegram_handler_reports_delivery_failure(monkeypatch):
    tmp_dir = _workspace_tmp_dir("telegram-delivery-failure")
    try:
        calls = []

        class Bot:
            def __init__(self):
                self.response_messages = 0

            async def send_chat_action(self, **kwargs):
                calls.append(("action", kwargs))

            async def send_message(self, **kwargs):
                calls.append(("message", kwargs))
                if kwargs["text"] == "Reply":
                    self.response_messages += 1
                    raise RuntimeError("telegram send failed")

        class SuccessfulBridge(TelegramAgentBridge):
            async def run_chat_message(self, **_kwargs):
                return TelegramTurnResult(conversation_id="telegram_test", reply="Reply")

        async def fake_prepare(*_args, **_kwargs):
            return telegram_bridge.TelegramPreparedInput(text="hello")

        bridge = SuccessfulBridge(
            session_store=TelegramSessionStore(tmp_dir / "sessions.json"),
            runner=lambda **_kwargs: None,
            initialize_conversation=lambda _conv_id, _title: None,
        )
        context = SimpleNamespace(
            bot=Bot(),
            application=SimpleNamespace(bot_data={"agent_bridge": bridge}),
        )
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=12345),
            effective_message=SimpleNamespace(message_thread_id=None),
            effective_user=SimpleNamespace(full_name="Kai", username="kai", id=99),
        )

        monkeypatch.setattr(telegram_bridge, "prepare_telegram_message_input", fake_prepare)

        asyncio.run(_handle_authorized_message(update, context))

        assert (
            "message",
            {
                "chat_id": 12345,
                "text": "The agent finished, but Telegram delivery failed: telegram send failed",
            },
        ) in calls
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
