import asyncio
import logging
import mimetypes
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from telegram import BotCommand, Update
from telegram.error import BadRequest, TelegramError
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from app.config import settings
from app.agent.identity import DEFAULT_AGENT_NAME
from app.agent.response_attachments import register_attachment_path
from app.agent.runtime_paths import runtime_path
from app.agent.settings_store import load_agent_settings
from app.integrations.telegram.agent_bridge import (
    TelegramAgentBridge,
    TelegramEffortSummary,
    TelegramModelSummary,
    split_telegram_message,
)
from app.agent.speech_to_text import (
    MAX_TRANSCRIPTION_BYTES,
    VOICE_MODEL_NOT_READY_MESSAGE,
    SpeechToTextCloudNotConfigured,
    SpeechToTextModelMissing,
    transcribe_audio_file,
)

logger = logging.getLogger(__name__)

COMMANDS = [
    BotCommand("start", "Show bot status and current session"),
    BotCommand("help", "Show Telegram bot help"),
    BotCommand("status", "Show current chat and model"),
    BotCommand("whoami", "Show Telegram user and chat ids"),
    BotCommand("session", "Show the active chat name"),
    BotCommand("resume", "List or switch chat history"),
    BotCommand("models", "List or switch model"),
    BotCommand("modeldefault", "Use the desktop default model"),
    BotCommand("effort", "List or switch reasoning effort"),
    BotCommand("new", "Start a fresh Telegram conversation session"),
]
TELEGRAM_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
TELEGRAM_MAX_PHOTO_BYTES = 10 * 1024 * 1024
TELEGRAM_FILE_DOWNLOAD_TIMEOUT_SECONDS = 120
TELEGRAM_AUTH_CONFIG_KEY = "telegram_authorization"
TELEGRAM_CHAT_LOCKS_KEY = "telegram_chat_locks"
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


@dataclass(frozen=True)
class TelegramPreparedInput:
    text: str
    attachments: tuple[dict, ...] = ()


@dataclass(frozen=True)
class TelegramAuthorization:
    allowed_user_ids: frozenset[int]
    allowed_chat_ids: frozenset[int]
    allow_all: bool = False

    @property
    def is_configured(self) -> bool:
        return self.allow_all or bool(self.allowed_user_ids or self.allowed_chat_ids)

    def is_authorized(self, update: Update) -> bool:
        if self.allow_all:
            return True
        user = update.effective_user
        chat = update.effective_chat
        if user is not None and int(user.id) in self.allowed_user_ids:
            return True
        if chat is not None and int(chat.id) in self.allowed_chat_ids:
            return True
        return False


def _parse_id_set(raw: str, *, setting_name: str) -> frozenset[int]:
    values: set[int] = set()
    for part in str(raw or "").replace(";", ",").replace("\n", ",").split(","):
        token = part.strip()
        if not token:
            continue
        try:
            values.add(int(token))
        except ValueError as exc:
            raise RuntimeError(f"{setting_name} contains a non-integer Telegram id: {token!r}") from exc
    return frozenset(values)


def telegram_authorization_from_settings() -> TelegramAuthorization:
    authorization = TelegramAuthorization(
        allowed_user_ids=_parse_id_set(
            settings.telegram_allowed_user_ids,
            setting_name="TELEGRAM_ALLOWED_USER_IDS",
        ),
        allowed_chat_ids=_parse_id_set(
            settings.telegram_allowed_chat_ids,
            setting_name="TELEGRAM_ALLOWED_CHAT_IDS",
        ),
        allow_all=bool(settings.telegram_allow_all),
    )
    if authorization.allow_all:
        logger.warning("TELEGRAM_ALLOW_ALL is enabled; Telegram requests are not restricted by id.")
    return authorization


def _authorization(context: ContextTypes.DEFAULT_TYPE) -> TelegramAuthorization:
    configured = context.application.bot_data.get(TELEGRAM_AUTH_CONFIG_KEY)
    if isinstance(configured, TelegramAuthorization):
        return configured
    authorization = telegram_authorization_from_settings()
    context.application.bot_data[TELEGRAM_AUTH_CONFIG_KEY] = authorization
    return authorization


async def _ensure_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    authorization = _authorization(context)
    if authorization.is_authorized(update):
        return True

    chat = update.effective_chat
    user = update.effective_user
    logger.warning(
        "Rejected unauthorized Telegram update chat_id=%s user_id=%s",
        getattr(chat, "id", None),
        getattr(user, "id", None),
    )
    if chat is not None:
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text="This Telegram chat is not authorized for this agent.",
            )
        except TelegramError:
            logger.exception("Failed to send Telegram authorization rejection.")
    return False


def _agent_display_name() -> str:
    try:
        identity = getattr(load_agent_settings(settings), "identity", None)
        name = str(getattr(identity, "agent_name", "") or "").strip()
        return name or DEFAULT_AGENT_NAME
    except Exception:
        logger.exception("Failed to load configured agent identity for Telegram status.")
        return DEFAULT_AGENT_NAME


def _start_message() -> str:
    return (
        f"{_agent_display_name()} AI Agent is online and connected to your laptop.\n"
        "Send a message to use the same agent runtime as the desktop app.\n"
        "Commands: /status, /whoami, /session, /resume, /models, /modeldefault, /effort, /new, /help."
    )


def _help_message() -> str:
    return (
        "Telegram messages run through the same Monaw conversation runtime as the desktop app.\n\n"
        "Commands:\n"
        "/start - Show bot status and current session.\n"
        "/help - Show this command list.\n"
        "/status - Show current chat, model, and whether this chat uses a model override.\n"
        "/whoami - Show your Telegram user id, chat id, and topic/thread id for allowlists.\n"
        "/session - Show the active chat name.\n"
        "/session rename <name> - Rename the active chat.\n"
        "/resume - List recent chat history.\n"
        "/resume 2 - Switch to a listed chat by number.\n"
        "/resume <chat name> - Switch by chat title.\n"
        "/models - List available models.\n"
        "/models 2 - Switch this Telegram chat to a listed model.\n"
        "/models <model name> - Switch by model name.\n"
        "/modeldefault - Clear this Telegram chat's model override and use the desktop default.\n"
        "/effort - List reasoning effort options.\n"
        "/effort 4 - Switch this Telegram chat to a listed reasoning effort.\n"
        "/effort medium - Switch by effort name.\n"
        "/effort default - Clear this Telegram chat's effort override and use the desktop default.\n"
        "/new - Start a fresh Telegram conversation session.\n\n"
        "Files and photos are sent to the agent as attachments. Voice and audio messages are transcribed first. "
        "Desktop approvals still happen in the desktop app."
    )


def _message_thread_id(update: Update) -> int | None:
    message = update.effective_message
    return getattr(message, "message_thread_id", None) if message is not None else None


def _chat_title(update: Update) -> str:
    chat = update.effective_chat
    if chat is None:
        return ""
    return (
        getattr(chat, "title", None)
        or getattr(chat, "full_name", "")
        or getattr(chat, "username", "")
        or str(getattr(chat, "id", ""))
    )


def _sender_name(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return ""
    return user.full_name or user.username or str(user.id)


def _agent_bridge(context: ContextTypes.DEFAULT_TYPE) -> TelegramAgentBridge:
    bridge = context.application.bot_data.get("agent_bridge")
    if not isinstance(bridge, TelegramAgentBridge):
        bridge = TelegramAgentBridge()
        context.application.bot_data["agent_bridge"] = bridge
    return bridge


def _safe_filename(filename: str) -> str:
    cleaned = _UNSAFE_FILENAME_RE.sub("_", str(filename or "")).strip(" .")
    return cleaned[:160] or "telegram-upload"


def _file_size(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _attachment_summary(attachments: list[dict]) -> str:
    if not attachments:
        return ""
    lines = ["Uploaded file(s):"]
    for attachment in attachments:
        name = str(attachment.get("name") or Path(str(attachment.get("path") or "")).name or "file")
        mime_type = str(attachment.get("mime_type") or "application/octet-stream")
        path = str(attachment.get("path") or "")
        lines.append(f"- {name} ({mime_type}): {path}")
    return "\n".join(lines)


async def _download_telegram_file(bot, file_id: str, target_path: Path) -> None:
    async def download() -> None:
        telegram_file = await bot.get_file(file_id)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        await telegram_file.download_to_drive(custom_path=str(target_path))

    try:
        await asyncio.wait_for(download(), timeout=TELEGRAM_FILE_DOWNLOAD_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise TimeoutError("Telegram file download timed out. Please try a smaller file or resend it.") from exc


async def _download_attachment(
    bot,
    *,
    conversation_id: str,
    file_id: str,
    filename: str,
    mime_type: str = "",
    size: int = 0,
    width: int | None = None,
    height: int | None = None,
) -> dict:
    if size > TELEGRAM_MAX_ATTACHMENT_BYTES:
        raise ValueError(f"Telegram file exceeds the {TELEGRAM_MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB upload limit.")
    upload_id = str(uuid.uuid4())
    safe_name = _safe_filename(filename)
    target_path = runtime_path("uploads", conversation_id or "telegram", upload_id, safe_name)
    await _download_telegram_file(bot, file_id, target_path)
    actual_size = target_path.stat().st_size if target_path.exists() else size
    resolved_mime = mime_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    register_attachment_path(upload_id, target_path)
    attachment = {
        "id": upload_id,
        "name": safe_name,
        "path": str(target_path),
        "mime_type": resolved_mime,
        "size": actual_size,
    }
    if width:
        attachment["width"] = int(width)
    if height:
        attachment["height"] = int(height)
    return attachment


async def _download_audio_for_transcription(
    bot,
    *,
    conversation_id: str,
    file_id: str,
    filename: str,
    size: int = 0,
) -> Path:
    if size > MAX_TRANSCRIPTION_BYTES:
        raise ValueError("Telegram audio exceeds the 25 MB transcription limit.")
    upload_id = str(uuid.uuid4())
    safe_name = _safe_filename(filename)
    target_path = runtime_path("uploads", conversation_id or "telegram", upload_id, safe_name)
    await _download_telegram_file(bot, file_id, target_path)
    if target_path.stat().st_size > MAX_TRANSCRIPTION_BYTES:
        raise ValueError("Telegram audio exceeds the 25 MB transcription limit.")
    return target_path


def _largest_photo(photo_sizes) -> object | None:
    photos = list(photo_sizes or [])
    if not photos:
        return None
    return max(
        photos,
        key=lambda item: (
            _file_size(getattr(item, "file_size", 0)),
            int(getattr(item, "width", 0) or 0) * int(getattr(item, "height", 0) or 0),
        ),
    )


async def prepare_telegram_message_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    conversation_id: str,
) -> TelegramPreparedInput:
    message = update.effective_message
    if message is None:
        return TelegramPreparedInput(text="")

    text_parts: list[str] = []
    message_text = str(getattr(message, "text", "") or "").strip()
    caption = str(getattr(message, "caption", "") or "").strip()
    if message_text:
        text_parts.append(message_text)
    elif caption:
        text_parts.append(caption)

    attachments: list[dict] = []
    photo = _largest_photo(getattr(message, "photo", None))
    if photo is not None:
        unique_id = str(getattr(photo, "file_unique_id", "") or getattr(photo, "file_id", "") or uuid.uuid4().hex[:12])
        attachments.append(
            await _download_attachment(
                context.bot,
                conversation_id=conversation_id,
                file_id=str(getattr(photo, "file_id")),
                filename=f"telegram-photo-{unique_id}.jpg",
                mime_type="image/jpeg",
                size=_file_size(getattr(photo, "file_size", 0)),
                width=int(getattr(photo, "width", 0) or 0) or None,
                height=int(getattr(photo, "height", 0) or 0) or None,
            )
        )

    document = getattr(message, "document", None)
    if document is not None:
        file_name = str(getattr(document, "file_name", "") or "telegram-document")
        mime_type = str(getattr(document, "mime_type", "") or mimetypes.guess_type(file_name)[0] or "application/octet-stream")
        if mime_type.startswith("audio/"):
            audio_path = await _download_audio_for_transcription(
                context.bot,
                conversation_id=conversation_id,
                file_id=str(getattr(document, "file_id")),
                filename=file_name,
                size=_file_size(getattr(document, "file_size", 0)),
            )
            transcript = await transcribe_audio_file(audio_path)
            text_parts.append(f"Audio transcript:\n{transcript}")
        else:
            attachments.append(
                await _download_attachment(
                    context.bot,
                    conversation_id=conversation_id,
                    file_id=str(getattr(document, "file_id")),
                    filename=file_name,
                    mime_type=mime_type,
                    size=_file_size(getattr(document, "file_size", 0)),
                )
            )

    voice = getattr(message, "voice", None)
    audio = getattr(message, "audio", None)
    audio_source = voice or audio
    if audio_source is not None:
        unique_id = str(getattr(audio_source, "file_unique_id", "") or getattr(audio_source, "file_id", "") or uuid.uuid4().hex[:12])
        fallback_name = "telegram-voice" if voice is not None else "telegram-audio"
        mime_type = str(getattr(audio_source, "mime_type", "") or "")
        extension = mimetypes.guess_extension(mime_type) or (".ogg" if voice is not None else ".mp3")
        filename = str(getattr(audio_source, "file_name", "") or f"{fallback_name}-{unique_id}{extension}")
        audio_path = await _download_audio_for_transcription(
            context.bot,
            conversation_id=conversation_id,
            file_id=str(getattr(audio_source, "file_id")),
            filename=filename,
            size=_file_size(getattr(audio_source, "file_size", 0)),
        )
        transcript = await transcribe_audio_file(audio_path)
        label = "Voice transcript" if voice is not None else "Audio transcript"
        text_parts.append(f"{label}:\n{transcript}")

    attachment_context = _attachment_summary(attachments)
    if attachment_context:
        text_parts.append(attachment_context)
    text = "\n\n".join(part for part in text_parts if part.strip()).strip()
    if not text and attachments:
        text = "Please review the uploaded file(s)."
    return TelegramPreparedInput(text=text, attachments=tuple(attachments))


async def register_bot_commands(bot) -> None:
    await bot.set_my_commands(COMMANDS)


async def send_response_attachments(bot, chat_id: int | str, attachments: tuple[dict, ...] | list[dict]) -> None:
    async def send_document_fallback(path: Path, caption: str) -> None:
        with path.open("rb") as document_handle:
            await bot.send_document(
                chat_id=chat_id,
                document=document_handle,
                filename=path.name,
                caption=caption,
            )

    for attachment in attachments:
        path = Path(str(attachment.get("path") or ""))
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = int(attachment.get("size") or 0)
        if size > TELEGRAM_MAX_ATTACHMENT_BYTES:
            await bot.send_message(
                chat_id=chat_id,
                text=f"File is too large to send through Telegram: {path.name}\n{path}",
            )
            continue

        mime_type = str(attachment.get("mime_type") or "")
        caption = path.name[:1024]
        try:
            if mime_type.startswith("image/") and size <= TELEGRAM_MAX_PHOTO_BYTES:
                try:
                    with path.open("rb") as photo_handle:
                        await bot.send_photo(chat_id=chat_id, photo=photo_handle, caption=caption)
                except BadRequest as exc:
                    if "photo_invalid_dimensions" not in str(exc).lower():
                        raise
                    await send_document_fallback(path, caption)
            else:
                await send_document_fallback(path, caption)
        except TelegramError as exc:
            await bot.send_message(
                chat_id=chat_id,
                text=f"Could not send attachment through Telegram: {path.name}\n{exc}",
            )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return
    if not await _ensure_authorized(update, context):
        return
    await register_bot_commands(context.bot)
    session = await asyncio.to_thread(
        _agent_bridge(context).current_session_summary,
        update.effective_chat.id,
        _message_thread_id(update),
        chat_title=_chat_title(update),
        sender_name=_sender_name(update),
    )
    start_message = await asyncio.to_thread(_start_message)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"{start_message}\n\nCurrent chat: {session.title}",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return
    if not await _ensure_authorized(update, context):
        return
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=_help_message(),
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return
    if not await _ensure_authorized(update, context):
        return
    bridge = _agent_bridge(context)
    thread_id = _message_thread_id(update)
    session, model, effort = await asyncio.gather(
        asyncio.to_thread(
            bridge.current_session_summary,
            update.effective_chat.id,
            thread_id,
            chat_title=_chat_title(update),
            sender_name=_sender_name(update),
        ),
        asyncio.to_thread(
            bridge.current_model_summary,
            update.effective_chat.id,
            thread_id,
        ),
        asyncio.to_thread(
            bridge.current_effort_summary,
            update.effective_chat.id,
            thread_id,
        ),
    )
    model_source = "Telegram override" if model.is_override else "Desktop default"
    effort_source = "Telegram override" if effort.is_override else "Desktop default"
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"Current chat: {session.title}\n"
            f"Model: {_format_model_label(model)}\n"
            f"Model source: {model_source}\n"
            f"Effort: {_format_effort_label(effort.effort)}\n"
            f"Effort source: {effort_source}"
        ),
    )


async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return
    if not await _ensure_authorized(update, context):
        return
    user = update.effective_user
    thread_id = _message_thread_id(update)
    lines = [
        f"Chat id: {update.effective_chat.id}",
        f"User id: {getattr(user, 'id', '') or 'unknown'}",
    ]
    if thread_id is not None:
        lines.append(f"Thread id: {thread_id}")
    lines.append("")
    lines.append("Use these ids in Settings -> Connections -> Telegram allowlists.")
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="\n".join(lines),
    )


async def session_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return
    if not await _ensure_authorized(update, context):
        return
    args = getattr(context, "args", []) or []
    if args and str(args[0]).strip().casefold() == "rename":
        new_title = " ".join(str(part) for part in args[1:]).strip()
        session, error = await asyncio.to_thread(
            _agent_bridge(context).rename_current_session,
            update.effective_chat.id,
            new_title,
            _message_thread_id(update),
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Renamed chat: {session.title}" if session is not None else error,
        )
        return
    session = await asyncio.to_thread(
        _agent_bridge(context).current_session_summary,
        update.effective_chat.id,
        _message_thread_id(update),
        chat_title=_chat_title(update),
        sender_name=_sender_name(update),
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"Current chat: {session.title}\n"
            "Use /session rename <new name> to rename it. Use /resume to list recent chat names."
        ),
    )


def _format_resume_options(options) -> str:
    if not options:
        return "No chat history found yet."
    lines = ["Recent chat history:"]
    for option in options:
        marker = " (current)" if option.is_current else ""
        lines.append(f"{option.index}. {option.title}{marker}")
    lines.append("")
    lines.append("Use /resume 2 or /resume <chat name> to switch.")
    return "\n".join(lines)


def _model_provider_label(provider: str) -> str:
    normalized = provider.strip().lower()
    if normalized == "deepseek":
        return "DeepSeek"
    if normalized == "gemini":
        return "Google"
    return "OpenAI"


def _format_model_label(model: TelegramModelSummary) -> str:
    return f"{_model_provider_label(model.provider)} - {model.model_name}"


def _format_effort_label(effort: str) -> str:
    normalized = str(effort or "").strip().lower()
    if normalized == "xhigh":
        return "X-High"
    return normalized.capitalize() if normalized else "Medium"


def _format_model_options(
    options: list[TelegramModelSummary],
    current: TelegramModelSummary,
) -> str:
    if not options:
        return "No models are configured."

    lines = [
        f"Current model: {_format_model_label(current)}",
    ]
    if current.is_override:
        lines.append("This chat is using its own model selection.")
    else:
        lines.append("This chat is using the desktop app default model.")
    lines.append("")
    lines.append("Available models:")
    for option in options:
        marker = " (current)" if option.is_current else ""
        lines.append(f"{option.index}. {_format_model_label(option)}{marker}")
    lines.append("")
    lines.append("Use /models 2 or /models <model name> to switch.")
    return "\n".join(lines)


def _format_effort_options(
    options: list[TelegramEffortSummary],
    current: TelegramEffortSummary,
) -> str:
    if not options:
        return "No reasoning effort options are configured."

    lines = [
        f"Current effort: {_format_effort_label(current.effort)}",
    ]
    if current.is_override:
        lines.append("This chat is using its own reasoning effort.")
    else:
        lines.append("This chat is using the desktop app default reasoning effort.")
    lines.append("")
    lines.append("Available efforts:")
    for option in options:
        marker = " (current)" if option.is_current else ""
        lines.append(f"{option.index}. {_format_effort_label(option.effort)}{marker}")
    lines.append("")
    lines.append("Use /effort 4 or /effort medium to switch. Use /effort default to clear the override.")
    return "\n".join(lines)


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return
    if not await _ensure_authorized(update, context):
        return
    bridge = _agent_bridge(context)
    selector = " ".join(getattr(context, "args", []) or []).strip()
    if not selector:
        options = await asyncio.to_thread(
            bridge.list_resume_options,
            update.effective_chat.id,
            _message_thread_id(update),
            limit=10,
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=_format_resume_options(options),
        )
        return

    session, error = await asyncio.to_thread(
        bridge.resume_session,
        update.effective_chat.id,
        selector,
        _message_thread_id(update),
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"Resumed chat: {session.title}" if session is not None else error,
    )


async def models_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return
    if not await _ensure_authorized(update, context):
        return
    bridge = _agent_bridge(context)
    selector = " ".join(getattr(context, "args", []) or []).strip()
    if not selector:
        current = await asyncio.to_thread(
            bridge.current_model_summary,
            update.effective_chat.id,
            _message_thread_id(update),
        )
        options = await asyncio.to_thread(
            bridge.list_model_options,
            update.effective_chat.id,
            _message_thread_id(update),
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=_format_model_options(options, current),
        )
        return

    model, error = await asyncio.to_thread(
        bridge.set_model_selection,
        update.effective_chat.id,
        selector,
        _message_thread_id(update),
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"Using model: {_format_model_label(model)}" if model is not None else error,
    )


async def modeldefault_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return
    if not await _ensure_authorized(update, context):
        return
    model = await asyncio.to_thread(
        _agent_bridge(context).clear_model_selection,
        update.effective_chat.id,
        _message_thread_id(update),
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"Using desktop default model: {_format_model_label(model)}",
    )


async def effort_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return
    if not await _ensure_authorized(update, context):
        return
    bridge = _agent_bridge(context)
    selector = " ".join(getattr(context, "args", []) or []).strip()
    if not selector:
        current = await asyncio.to_thread(
            bridge.current_effort_summary,
            update.effective_chat.id,
            _message_thread_id(update),
        )
        options = await asyncio.to_thread(
            bridge.list_effort_options,
            update.effective_chat.id,
            _message_thread_id(update),
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=_format_effort_options(options, current),
        )
        return

    if selector.casefold() in {"default", "desktop", "clear", "reset"}:
        effort = await asyncio.to_thread(
            bridge.clear_effort_selection,
            update.effective_chat.id,
            _message_thread_id(update),
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Using desktop default effort: {_format_effort_label(effort.effort)}",
        )
        return

    effort, error = await asyncio.to_thread(
        bridge.set_effort_selection,
        update.effective_chat.id,
        selector,
        _message_thread_id(update),
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"Using effort: {_format_effort_label(effort.effort)}" if effort is not None else error,
    )


async def new_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return
    if not await _ensure_authorized(update, context):
        return
    session = await asyncio.to_thread(
        _agent_bridge(context).start_new_session_summary,
        update.effective_chat.id,
        _message_thread_id(update),
        chat_title=_chat_title(update),
        sender_name=_sender_name(update),
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"Started a new chat: {session.title}",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_message is None:
        return
    if not await _ensure_authorized(update, context):
        return
    lock = _message_lock(update, context)
    if lock.locked():
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="A previous Telegram request is still running for this chat. Please wait for it to finish.",
        )
        return
    async with lock:
        await _handle_authorized_message(update, context)


def _message_lock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> asyncio.Lock:
    locks = context.application.bot_data.setdefault(TELEGRAM_CHAT_LOCKS_KEY, {})
    key = (int(update.effective_chat.id), _message_thread_id(update))
    lock = locks.get(key)
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        locks[key] = lock
    return lock


async def _handle_authorized_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    bridge = _agent_bridge(context)
    conversation_id = await asyncio.to_thread(
        bridge.current_session,
        update.effective_chat.id,
        _message_thread_id(update),
    )
    try:
        prepared = await prepare_telegram_message_input(
            update,
            context,
            conversation_id=conversation_id,
        )
    except (SpeechToTextModelMissing, SpeechToTextCloudNotConfigured) as exc:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=str(exc) or VOICE_MODEL_NOT_READY_MESSAGE,
        )
        return
    except Exception as exc:
        logger.exception("Failed to prepare Telegram message input.")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Could not process this Telegram upload: {exc}",
        )
        return
    if not prepared.text and not prepared.attachments:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Send a text message, photo, file, or voice message.",
        )
        return

    pending_notice_sent = False

    async def relay_event(event: dict) -> None:
        nonlocal pending_notice_sent
        if pending_notice_sent:
            return
        if event.get("event") in {"approval_required", "access_grant_required"}:
            pending_notice_sent = True
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Approval is required in the desktop app before this Telegram request can continue.",
            )

    try:
        result = await bridge.run_chat_message(
            chat_id=update.effective_chat.id,
            thread_id=_message_thread_id(update),
            chat_title=_chat_title(update),
            sender_name=_sender_name(update),
            text=prepared.text,
            attachments=list(prepared.attachments),
            on_event=relay_event,
        )
    except Exception as exc:
        logger.exception("Telegram agent request failed before a response was produced.")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Telegram request failed before I could produce a response: {exc}",
        )
        return

    try:
        for chunk in split_telegram_message(result.reply):
            await context.bot.send_message(chat_id=update.effective_chat.id, text=chunk)
        if result.attachments:
            await send_response_attachments(context.bot, update.effective_chat.id, result.attachments)
    except Exception as exc:
        logger.exception("Failed to send Telegram agent response.")
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"The agent finished, but Telegram delivery failed: {exc}",
            )
        except Exception:
            logger.exception("Failed to send Telegram delivery failure notice.")


def build_application(token: str | None = None):
    bot_token = (token or settings.telegram_bot_token).strip()
    if not bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured.")
    authorization = telegram_authorization_from_settings()
    if not authorization.is_configured:
        raise RuntimeError(
            "Telegram bot requires TELEGRAM_ALLOWED_USER_IDS or TELEGRAM_ALLOWED_CHAT_IDS. "
            "Set TELEGRAM_ALLOW_ALL=true only for explicitly unsafe local testing."
        )

    application = ApplicationBuilder().token(bot_token).build()
    application.bot_data[TELEGRAM_AUTH_CONFIG_KEY] = authorization
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("whoami", whoami_command))
    application.add_handler(CommandHandler("session", session_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("models", models_command))
    application.add_handler(CommandHandler("modeldefault", modeldefault_command))
    application.add_handler(CommandHandler("effort", effort_command))
    application.add_handler(CommandHandler("new", new_session))
    application.add_handler(
        MessageHandler(
            (
                filters.TEXT
                | filters.PHOTO
                | filters.Document.ALL
                | filters.VOICE
                | filters.AUDIO
            )
            & (~filters.COMMAND),
            handle_message,
        )
    )
    return application
