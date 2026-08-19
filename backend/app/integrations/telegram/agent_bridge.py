from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.agent.llm_constants import CHAT_MODELS_BY_PROVIDER, DEFAULT_OPENAI_CHAT_MODEL
from app.agent.database import get_db
from app.agent.response_attachments import collect_response_attachments
from app.agent.settings_store import build_runtime_namespace, load_agent_settings
from app.config import settings as app_settings
from app.integrations.telegram.session import TelegramSessionStore

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_SAFE_CHUNK_SIZE = 3800
TELEGRAM_RATE_LIMIT_WINDOW_SECONDS = 60.0
TELEGRAM_CHAT_RATE_LIMIT = 12
TELEGRAM_USER_RATE_LIMIT = 8
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

EventCallback = Callable[[dict], Awaitable[None] | None]
AgentRunner = Callable[..., AsyncIterator[dict]]
SettingsFactory = Callable[[], Any]
ConversationInitializer = Callable[[str, str], None]
ConversationGetter = Callable[[str], dict[str, Any] | None]
ConversationLister = Callable[[], list[dict[str, Any]]]


_MARKDOWN_LINK_RE = re.compile(r"!?\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_HTML_TAG_RE = re.compile(r"</?[^>\n]+>")


@dataclass(frozen=True)
class TelegramTurnResult:
    conversation_id: str
    reply: str
    attachments: tuple[dict[str, Any], ...] = ()
    status: str = "complete"
    incomplete: bool = False
    reason_code: str = ""


@dataclass(frozen=True)
class TelegramSessionSummary:
    conversation_id: str
    title: str
    index: int = 0
    is_current: bool = False


@dataclass(frozen=True)
class TelegramModelSummary:
    provider: str
    model_name: str
    index: int = 0
    is_current: bool = False
    is_override: bool = False


@dataclass(frozen=True)
class TelegramEffortSummary:
    effort: str
    index: int = 0
    is_current: bool = False
    is_override: bool = False


def build_runtime_settings():
    return build_runtime_namespace(app_settings, load_agent_settings(app_settings))


async def run_agent_stream(*args: Any, **kwargs: Any) -> AsyncIterator[dict]:
    from app.agent.runtime import run_agent_stream as runtime_run_agent_stream

    kwargs.setdefault("execution_source", "telegram")
    kwargs.setdefault("control_session_id", "telegram")
    kwargs.setdefault("principal_id", "telegram")
    kwargs.setdefault("permission_profile_id", "telegram-restricted")
    kwargs.setdefault("interactive", False)
    async for event in runtime_run_agent_stream(*args, **kwargs):
        yield event


def split_telegram_message(text: str, *, max_length: int = TELEGRAM_SAFE_CHUNK_SIZE) -> list[str]:
    content = str(text or "").strip() or "Done."
    if len(content) <= max_length:
        return [content]

    chunks: list[str] = []
    remaining = content
    while len(remaining) > max_length:
        split_at = remaining.rfind("\n\n", 0, max_length)
        if split_at < max_length // 2:
            split_at = remaining.rfind("\n", 0, max_length)
        if split_at < max_length // 2:
            split_at = remaining.rfind(" ", 0, max_length)
        if split_at < max_length // 2:
            split_at = max_length
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def simplify_telegram_text(text: str) -> str:
    """Convert rich Markdown-ish agent output into stable Telegram plain text."""
    content = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not content:
        return "Done."

    content = re.sub(r"```[A-Za-z0-9_-]*\n(.*?)```", r"\1", content, flags=re.DOTALL)
    content = re.sub(r"`([^`\n]+)`", r"\1", content)
    content = _MARKDOWN_LINK_RE.sub(lambda match: match.group(1) or match.group(2), content)
    content = _HTML_TAG_RE.sub("", content)

    lines: list[str] = []
    previous_blank = False
    for raw_line in content.split("\n"):
        line = raw_line.strip()
        if not line:
            if lines and not previous_blank:
                lines.append("")
            previous_blank = True
            continue

        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)
        line = re.sub(r"^\s{0,3}>\s?", "", line)
        line = re.sub(r"^\s*[-*+]\s+", "- ", line)
        line = re.sub(r"^\s*(\d+)[.)]\s+", r"\1. ", line)
        line = re.sub(r"^\s*\|?(.*?)\|?\s*$", r"\1", line) if "|" in line else line
        line = line.replace("|", " - ")
        line = re.sub(r"(\*\*|__)(.*?)\1", r"\2", line)
        line = re.sub(r"(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)", r"\1", line)
        line = re.sub(r"(?<!_)_(?!_)(.*?)(?<!_)_(?!_)", r"\1", line)
        line = re.sub(r"~~(.*?)~~", r"\1", line)
        line = re.sub(r"\\([`*_{}\[\]()#+!|>])", r"\1", line)
        line = re.sub(r"\s{2,}", " ", line).strip()
        if line and not re.fullmatch(r"-{3,}|={3,}", line):
            lines.append(line)
            previous_blank = False

    simplified = "\n".join(lines).strip()
    simplified = re.sub(r"\n{3,}", "\n\n", simplified)
    return simplified or "Done."


def _default_conversation_initializer(conversation_id: str, title: str) -> None:
    db = get_db()
    existing = db.get_conversation(conversation_id)
    clean_title = title.strip() or "Telegram chat"
    if existing is None:
        db.create_conversation(conversation_id, clean_title)
    elif existing.get("title") in {"", "Untitled"}:
        db.update_conversation(conversation_id, title=clean_title)


def _default_conversation_getter(conversation_id: str) -> dict[str, Any] | None:
    return get_db().get_conversation(conversation_id)


def _default_conversation_lister() -> list[dict[str, Any]]:
    return get_db().list_conversations()


def _telegram_title(chat_id: int | str, chat_title: str = "", sender_name: str = "") -> str:
    label = chat_title.strip() or sender_name.strip() or str(chat_id)
    return f"Telegram - {label}"[:200]


def _display_title(conversation: dict[str, Any] | None, fallback: str = "Telegram chat") -> str:
    title = str((conversation or {}).get("title") or "").strip()
    if title and title != "Untitled":
        return title
    return fallback.strip() or "Telegram chat"


def _flatten_model_options() -> list[TelegramModelSummary]:
    options: list[TelegramModelSummary] = []
    for provider, model_names in CHAT_MODELS_BY_PROVIDER.items():
        for model_name in model_names:
            options.append(
                TelegramModelSummary(
                    provider=provider,
                    model_name=model_name,
                    index=len(options) + 1,
                )
            )
    return options


class TelegramAgentBridge:
    def __init__(
        self,
        *,
        session_store: TelegramSessionStore | None = None,
        settings_factory: SettingsFactory = build_runtime_settings,
        runner: AgentRunner = run_agent_stream,
        initialize_conversation: ConversationInitializer = _default_conversation_initializer,
        get_conversation: ConversationGetter = _default_conversation_getter,
        list_conversations: ConversationLister = _default_conversation_lister,
    ) -> None:
        self.session_store = session_store or TelegramSessionStore()
        self.settings_factory = settings_factory
        self.runner = runner
        self.initialize_conversation = initialize_conversation
        self.get_conversation = get_conversation
        self.list_conversations = list_conversations
        self._rate_limit_window_seconds = TELEGRAM_RATE_LIMIT_WINDOW_SECONDS
        self._chat_rate_limit = TELEGRAM_CHAT_RATE_LIMIT
        self._user_rate_limit = TELEGRAM_USER_RATE_LIMIT
        self._rate_limit_events: dict[str, deque[float]] = {}

    def current_session(self, chat_id: int | str, thread_id: int | str | None = None) -> str:
        return self.session_store.get_current(chat_id, thread_id)

    def _rate_limit_exceeded(self, *, chat_id: int | str, sender_name: str = "") -> str:
        now = time.monotonic()
        checks = [
            (f"chat:{chat_id}", self._chat_rate_limit, "telegram_chat_rate_limited"),
        ]
        sender = str(sender_name or "unknown").strip() or "unknown"
        checks.append((f"user:{chat_id}:{sender}", self._user_rate_limit, "telegram_user_rate_limited"))
        for key, limit, reason_code in checks:
            events = self._rate_limit_events.setdefault(key, deque())
            while events and now - events[0] > self._rate_limit_window_seconds:
                events.popleft()
            if len(events) >= limit:
                return reason_code
        for key, _limit, _reason_code in checks:
            self._rate_limit_events[key].append(now)
        return ""

    def _initialize_current_conversation(
        self,
        chat_id: int | str,
        thread_id: int | str | None,
        chat_title: str = "",
        sender_name: str = "",
    ) -> str:
        conversation_id = self.current_session(chat_id, thread_id)
        self.initialize_conversation(
            conversation_id,
            _telegram_title(chat_id, chat_title=chat_title, sender_name=sender_name),
        )
        return conversation_id

    def _base_model_selection(self) -> TelegramModelSummary:
        runtime_settings = self.settings_factory()
        provider = str(getattr(runtime_settings, "model_provider", "") or "openai").strip().lower()
        if provider not in CHAT_MODELS_BY_PROVIDER:
            provider = "openai"
        model_name = str(getattr(runtime_settings, "model_name", "") or "").strip()
        provider_models = CHAT_MODELS_BY_PROVIDER.get(provider, ())
        if not model_name:
            model_name = provider_models[0] if provider_models else DEFAULT_OPENAI_CHAT_MODEL
        return TelegramModelSummary(provider=provider, model_name=model_name)

    def current_model_summary(
        self,
        chat_id: int | str,
        thread_id: int | str | None = None,
    ) -> TelegramModelSummary:
        conversation_id = self.current_session(chat_id, thread_id)
        stored = self.session_store.get_model_selection(conversation_id)
        if stored:
            return TelegramModelSummary(
                provider=stored["provider"],
                model_name=stored["model_name"],
                is_current=True,
                is_override=True,
            )
        base = self._base_model_selection()
        return TelegramModelSummary(
            provider=base.provider,
            model_name=base.model_name,
            is_current=True,
            is_override=False,
        )

    def list_model_options(
        self,
        chat_id: int | str,
        thread_id: int | str | None = None,
    ) -> list[TelegramModelSummary]:
        current = self.current_model_summary(chat_id, thread_id)
        summaries: list[TelegramModelSummary] = []
        for option in _flatten_model_options():
            is_current = option.provider == current.provider and option.model_name == current.model_name
            summaries.append(
                TelegramModelSummary(
                    provider=option.provider,
                    model_name=option.model_name,
                    index=option.index,
                    is_current=is_current,
                    is_override=is_current and current.is_override,
                )
            )
        return summaries

    def set_model_selection(
        self,
        chat_id: int | str,
        selector: str,
        thread_id: int | str | None = None,
    ) -> tuple[TelegramModelSummary | None, str]:
        value = selector.strip()
        if not value:
            return None, "Send /models to list models, then /models 2 or /models <model name>."

        options = _flatten_model_options()
        selected: TelegramModelSummary | None = None
        if value.isdigit():
            number = int(value)
            selected = next((item for item in options if item.index == number), None)
            if selected is None:
                return None, f"No model found for number {number}. Send /models to see the current list."
        else:
            normalized = value.casefold()
            exact_matches = [
                item
                for item in options
                if item.model_name.casefold() == normalized
                or f"{item.provider}:{item.model_name}".casefold() == normalized
            ]
            if len(exact_matches) == 1:
                selected = exact_matches[0]
            elif len(exact_matches) > 1:
                return None, f"Multiple models are named {value!r}. Use /models and choose by number."
            else:
                partial_matches = [
                    item
                    for item in options
                    if normalized in item.model_name.casefold()
                    or normalized in f"{item.provider}:{item.model_name}".casefold()
                ]
                if len(partial_matches) == 1:
                    selected = partial_matches[0]
                elif len(partial_matches) > 1:
                    return None, f"Multiple models match {value!r}. Use /models and choose by number."
                else:
                    return None, f"No model named {value!r}. Send /models to see available models."

        conversation_id = self.current_session(chat_id, thread_id)
        self.session_store.set_model_selection(
            conversation_id,
            provider=selected.provider,
            model_name=selected.model_name,
        )
        return (
            TelegramModelSummary(
                provider=selected.provider,
                model_name=selected.model_name,
                index=selected.index,
                is_current=True,
                is_override=True,
            ),
            "",
        )

    def clear_model_selection(
        self,
        chat_id: int | str,
        thread_id: int | str | None = None,
    ) -> TelegramModelSummary:
        conversation_id = self.current_session(chat_id, thread_id)
        self.session_store.clear_model_selection(conversation_id)
        base = self._base_model_selection()
        return TelegramModelSummary(
            provider=base.provider,
            model_name=base.model_name,
            is_current=True,
            is_override=False,
        )

    def _base_reasoning_effort(self) -> str:
        runtime_settings = self.settings_factory()
        value = str(getattr(runtime_settings, "reasoning_effort", "") or "").strip().lower()
        if value in REASONING_EFFORTS:
            return value
        llm_settings = getattr(runtime_settings, "llm", None)
        value = str(getattr(llm_settings, "reasoning_effort", "") or "").strip().lower()
        return value if value in REASONING_EFFORTS else "medium"

    def current_effort_summary(
        self,
        chat_id: int | str,
        thread_id: int | str | None = None,
    ) -> TelegramEffortSummary:
        conversation_id = self.current_session(chat_id, thread_id)
        stored = self.session_store.get_effort_selection(conversation_id)
        if stored in REASONING_EFFORTS:
            return TelegramEffortSummary(effort=stored, is_current=True, is_override=True)
        return TelegramEffortSummary(
            effort=self._base_reasoning_effort(),
            is_current=True,
            is_override=False,
        )

    def list_effort_options(
        self,
        chat_id: int | str,
        thread_id: int | str | None = None,
    ) -> list[TelegramEffortSummary]:
        current = self.current_effort_summary(chat_id, thread_id)
        return [
            TelegramEffortSummary(
                effort=effort,
                index=index,
                is_current=effort == current.effort,
                is_override=effort == current.effort and current.is_override,
            )
            for index, effort in enumerate(REASONING_EFFORTS, start=1)
        ]

    def set_effort_selection(
        self,
        chat_id: int | str,
        selector: str,
        thread_id: int | str | None = None,
    ) -> tuple[TelegramEffortSummary | None, str]:
        value = selector.strip().lower()
        if not value:
            return None, "Send /effort to list options, then /effort 4 or /effort medium."

        selected = ""
        if value.isdigit():
            number = int(value)
            if number < 1 or number > len(REASONING_EFFORTS):
                return None, f"No effort found for number {number}. Send /effort to see the current list."
            selected = REASONING_EFFORTS[number - 1]
        else:
            matches = [effort for effort in REASONING_EFFORTS if effort == value]
            if not matches:
                matches = [effort for effort in REASONING_EFFORTS if value in effort]
            if len(matches) == 1:
                selected = matches[0]
            elif len(matches) > 1:
                return None, f"Multiple effort levels match {selector!r}. Use /effort and choose by number."
            else:
                return None, f"No effort named {selector!r}. Send /effort to see available options."

        conversation_id = self.current_session(chat_id, thread_id)
        self.session_store.set_effort_selection(conversation_id, selected)
        return TelegramEffortSummary(
            effort=selected,
            index=REASONING_EFFORTS.index(selected) + 1,
            is_current=True,
            is_override=True,
        ), ""

    def clear_effort_selection(
        self,
        chat_id: int | str,
        thread_id: int | str | None = None,
    ) -> TelegramEffortSummary:
        conversation_id = self.current_session(chat_id, thread_id)
        self.session_store.clear_effort_selection(conversation_id)
        return TelegramEffortSummary(
            effort=self._base_reasoning_effort(),
            is_current=True,
            is_override=False,
        )

    def _settings_for_conversation(self, conversation_id: str):
        runtime_settings = self.settings_factory()
        stored_model = self.session_store.get_model_selection(conversation_id)
        stored_effort = self.session_store.get_effort_selection(conversation_id)
        if not stored_model and stored_effort not in REASONING_EFFORTS:
            return runtime_settings

        try:
            runtime_settings = copy.deepcopy(runtime_settings)
        except Exception:
            logger.exception("Falling back to shallow runtime settings copy for Telegram overrides.")
            runtime_settings = copy.copy(runtime_settings)

        if stored_model:
            provider = stored_model["provider"]
            model_name = stored_model["model_name"]
            try:
                setattr(runtime_settings, "model_provider", provider)
                setattr(runtime_settings, "model_name", model_name)
            except Exception:
                pass

        llm_settings = getattr(runtime_settings, "llm", None)
        if stored_model and llm_settings is not None:
            try:
                llm_copy = copy.deepcopy(llm_settings)
                setattr(llm_copy, "provider", stored_model["provider"])
                setattr(llm_copy, "model_name", stored_model["model_name"])
                setattr(runtime_settings, "llm", llm_copy)
            except Exception:
                pass
        llm_settings = getattr(runtime_settings, "llm", None)
        if stored_effort in REASONING_EFFORTS:
            try:
                setattr(runtime_settings, "reasoning_effort", stored_effort)
            except Exception:
                pass
            if llm_settings is not None:
                try:
                    llm_copy = copy.deepcopy(llm_settings)
                    setattr(llm_copy, "reasoning_effort", stored_effort)
                    setattr(runtime_settings, "llm", llm_copy)
                except Exception:
                    pass
        return runtime_settings

    def current_session_summary(
        self,
        chat_id: int | str,
        thread_id: int | str | None = None,
        *,
        chat_title: str = "",
        sender_name: str = "",
    ) -> TelegramSessionSummary:
        conversation_id = self.current_session(chat_id, thread_id)
        fallback_title = _telegram_title(chat_id, chat_title=chat_title, sender_name=sender_name)
        self.initialize_conversation(conversation_id, fallback_title)
        return TelegramSessionSummary(
            conversation_id=conversation_id,
            title=_display_title(self.get_conversation(conversation_id), fallback_title),
            is_current=True,
        )

    def rename_current_session(
        self,
        chat_id: int | str,
        title: str,
        thread_id: int | str | None = None,
    ) -> tuple[TelegramSessionSummary | None, str]:
        clean_title = " ".join(str(title or "").split())[:200]
        if not clean_title:
            return None, "Send /session rename <new chat name>."
        conversation_id = self.current_session(chat_id, thread_id)
        self.initialize_conversation(conversation_id, clean_title)
        try:
            get_db().update_conversation(conversation_id, title=clean_title)
        except Exception as exc:
            logger.exception("Failed to rename Telegram conversation.")
            return None, f"Could not rename chat: {exc}"
        return TelegramSessionSummary(
            conversation_id=conversation_id,
            title=clean_title,
            is_current=True,
        ), ""

    def start_new_session(self, chat_id: int | str, thread_id: int | str | None = None) -> str:
        return self.session_store.start_new(chat_id, thread_id)

    def start_new_session_summary(
        self,
        chat_id: int | str,
        thread_id: int | str | None = None,
        *,
        chat_title: str = "",
        sender_name: str = "",
    ) -> TelegramSessionSummary:
        conversation_id = self.start_new_session(chat_id, thread_id)
        title = _telegram_title(chat_id, chat_title=chat_title, sender_name=sender_name)
        self.initialize_conversation(conversation_id, title)
        return TelegramSessionSummary(
            conversation_id=conversation_id,
            title=_display_title(self.get_conversation(conversation_id), title),
            is_current=True,
        )

    def list_resume_options(
        self,
        chat_id: int | str,
        thread_id: int | str | None = None,
        *,
        limit: int = 10,
    ) -> list[TelegramSessionSummary]:
        current_id = self.current_session(chat_id, thread_id)
        summaries: list[TelegramSessionSummary] = []
        for index, conversation in enumerate(self.list_conversations()[: max(1, limit)], start=1):
            conversation_id = str(conversation.get("id") or "").strip()
            if not conversation_id:
                continue
            summaries.append(
                TelegramSessionSummary(
                    conversation_id=conversation_id,
                    title=_display_title(conversation),
                    index=index,
                    is_current=conversation_id == current_id,
                )
            )
        return summaries

    def resume_session(
        self,
        chat_id: int | str,
        selector: str,
        thread_id: int | str | None = None,
    ) -> tuple[TelegramSessionSummary | None, str]:
        value = selector.strip()
        if not value:
            return None, "Send /resume to list chats, then /resume 2 or /resume <chat name>."

        options = self.list_resume_options(chat_id, thread_id, limit=50)
        selected: TelegramSessionSummary | None = None
        if value.isdigit():
            number = int(value)
            selected = next((item for item in options if item.index == number), None)
            if selected is None:
                return None, f"No chat found for number {number}. Send /resume to see the current list."
        else:
            normalized = value.casefold()
            exact_matches = [item for item in options if item.title.casefold() == normalized]
            if len(exact_matches) == 1:
                selected = exact_matches[0]
            elif len(exact_matches) > 1:
                return None, f"Multiple chats are named {value!r}. Use /resume and choose by number."
            else:
                partial_matches = [item for item in options if normalized in item.title.casefold()]
                if len(partial_matches) == 1:
                    selected = partial_matches[0]
                elif len(partial_matches) > 1:
                    return None, f"Multiple chats match {value!r}. Use /resume and choose by number."
                else:
                    return None, f"No chat named {value!r}. Send /resume to see recent chat names."

        self.session_store.set_current(chat_id, selected.conversation_id, thread_id)
        return (
            TelegramSessionSummary(
                conversation_id=selected.conversation_id,
                title=selected.title,
                index=selected.index,
                is_current=True,
            ),
            "",
        )

    async def run_chat_message(
        self,
        *,
        chat_id: int | str,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
        thread_id: int | str | None = None,
        chat_title: str = "",
        sender_name: str = "",
        on_event: EventCallback | None = None,
    ) -> TelegramTurnResult:
        message = text.strip()
        input_attachments = [
            attachment
            for attachment in (attachments or [])
            if isinstance(attachment, dict)
        ]
        conversation_id = await asyncio.to_thread(
            self._initialize_current_conversation,
            chat_id,
            thread_id,
            chat_title,
            sender_name,
        )
        if not message and not input_attachments:
            return TelegramTurnResult(
                conversation_id=conversation_id,
                reply="Send a text message, photo, file, or voice message and I will continue this Telegram session.",
            )
        rate_limit_reason = self._rate_limit_exceeded(chat_id=chat_id, sender_name=sender_name)
        if rate_limit_reason:
            return TelegramTurnResult(
                conversation_id=conversation_id,
                reply="This Telegram source is sending messages too quickly. Try again shortly.",
                status="blocked",
                incomplete=True,
                reason_code=rate_limit_reason,
            )

        chunks: list[str] = []
        done_summary = ""
        status = "complete"
        incomplete = False
        reason_code = ""
        error_text = ""
        tool_outputs: list[str] = []
        attachments: list[dict[str, Any]] = []
        runtime_settings = await asyncio.to_thread(self._settings_for_conversation, conversation_id)

        try:
            async for event in self.runner(
                message=message,
                conversation_id=conversation_id,
                settings=runtime_settings,
                attachments=input_attachments,
                execution_source="telegram",
                control_session_id="telegram",
                principal_id=f"telegram:{chat_id}:{sender_name or 'unknown'}",
                permission_profile_id=f"telegram:{chat_id}:restricted",
                interactive=False,
            ):
                if on_event is not None:
                    callback_result = on_event(event)
                    if inspect.isawaitable(callback_result):
                        await callback_result
                event_name = str(event.get("event") or "")
                data = event.get("data", {})
                if not isinstance(data, dict):
                    data = {}
                if event_name == "token":
                    chunks.append(str(data.get("content") or ""))
                elif event_name == "done":
                    done_summary = str(data.get("summary") or "").strip()
                    status = str(data.get("status") or status or "complete")
                    incomplete = bool(data.get("incomplete"))
                    reason_code = str(data.get("reason_code") or "")
                    raw_attachments = data.get("attachments")
                    if isinstance(raw_attachments, list):
                        attachments = [
                            attachment
                            for attachment in raw_attachments
                            if isinstance(attachment, dict)
                        ]
                elif event_name == "error":
                    error_text = str(data.get("message") or data.get("error") or "Agent error.").strip()
                elif event_name == "tool_end":
                    tool_outputs.append(str(data.get("output") or ""))
        except Exception as exc:
            logger.exception("Telegram agent turn failed: %s", exc)
            error_text = str(exc) or "Agent error."
            status = "error"

        raw_reply = "".join(chunks).strip() or done_summary or error_text or "Done."
        if not attachments:
            attachments = collect_response_attachments(
                content=raw_reply,
                tool_outputs=tool_outputs,
            )
        reply = simplify_telegram_text(raw_reply)
        return TelegramTurnResult(
            conversation_id=conversation_id,
            reply=reply,
            attachments=tuple(attachments),
            status=status,
            incomplete=incomplete,
            reason_code=reason_code,
        )
