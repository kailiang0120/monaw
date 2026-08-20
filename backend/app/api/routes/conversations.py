import json
import uuid

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from app.agent.database import get_db
from app.agent.context_usage import (
    build_context_usage_report,
    infer_context_query,
    model_compaction_threshold,
    model_context_token_limit,
)
from app.agent.memory_manager import (
    create_conversation_file,
    delete_conversation_file,
    list_conversation_metadata,
    peek_memory_manager,
)
from app.agent.response_attachments import (
    ATTACHMENT_HANDLE_PREFIX,
    collect_response_attachments,
    new_attachment_id,
    public_attachment_payload,
    register_attachment_path,
)
from app.agent.settings_store import build_runtime_namespace, load_agent_settings
from app.agent.ui_events import publish_ui_event
from app.config import settings
from app.schemas import (
    ConversationCreate,
    ConversationOut,
    ConversationRename,
    ContextUsagePayload,
    MessageOut,
    MessagesResponse,
    OkResponse,
    ToolCallOut,
)

router = APIRouter()

HISTORY_TOOL_PAYLOAD_PREVIEW_CHARS = 1200
HISTORY_TOOL_PAYLOAD_TRUNCATED_SUFFIX = "\n\n[history preview truncated]"
TOOL_CALL_MODE_NONE = "none"
TOOL_CALL_MODE_SUMMARY = "summary"
TOOL_CALL_MODE_FULL = "full"
CONVERSATION_LIST_LIMIT_MAX = 200
MESSAGE_LIMIT_MAX = 200
MESSAGE_TOOL_CALL_LIMIT_MAX = 200
MESSAGE_TOOL_PAYLOAD_LIMIT_MAX = 20_000


def get_runtime(runtime_settings):
    from app.agent.runtime import get_runtime as runtime_get_runtime

    return runtime_get_runtime(runtime_settings)


def build_identity_prompt(runtime_settings) -> str:
    from app.agent.runtime import build_identity_prompt as runtime_build_identity_prompt

    return runtime_build_identity_prompt(runtime_settings)


def build_skill_prompt_sections(skills, visible_tool_names: set[str]) -> dict[str, str]:
    from app.agent.skill_prompt import build_skill_prompt_sections as skill_build_prompt_sections

    return skill_build_prompt_sections(skills, visible_tool_names)


def _stored_response_attachments(
    raw_value: object,
    *,
    control_session_id: str = "",
    conversation_id: str = "",
) -> list[dict] | None:
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    try:
        payload = json.loads(raw_value)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, list):
        return None
    attachments: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip()
        item_path = str(item.get("path") or "").strip()
        if item_id and item_path.startswith(ATTACHMENT_HANDLE_PREFIX):
            sanitized = dict(item)
            sanitized["path"] = f"{ATTACHMENT_HANDLE_PREFIX}{item_id}"
            sanitized.setdefault("conversation_id", conversation_id)
            attachments.append(sanitized)
            continue
        try:
            path = Path(item_path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if not path.is_file():
            continue
        record = register_attachment_path(
            new_attachment_id(),
            path,
            control_session_id=control_session_id,
            principal_id=control_session_id,
            conversation_id=conversation_id,
            mime_type=str(item.get("mime_type") or ""),
            width=item.get("width") if isinstance(item.get("width"), int) else None,
            height=item.get("height") if isinstance(item.get("height"), int) else None,
        )
        if record is not None:
            attachments.append(public_attachment_payload(record))
    return attachments


def _history_tool_payload(value: object, limit: int) -> str:
    text = str(value or "")
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + HISTORY_TOOL_PAYLOAD_TRUNCATED_SUFFIX


def _tool_call_mode(
    *,
    include_tool_calls: bool,
    tool_call_mode: str | None,
) -> str:
    requested = str(tool_call_mode or "").strip().lower()
    if requested in {TOOL_CALL_MODE_NONE, TOOL_CALL_MODE_SUMMARY, TOOL_CALL_MODE_FULL}:
        return requested
    return TOOL_CALL_MODE_FULL if include_tool_calls else TOOL_CALL_MODE_NONE


def _tool_call_preview(preview: object, original_length: object, limit: int) -> str:
    text = str(preview or "")
    try:
        length = max(0, int(original_length or 0))
    except (TypeError, ValueError):
        length = len(text)
    if limit <= 0:
        return ""
    if length <= limit:
        return text
    return text.rstrip() + HISTORY_TOOL_PAYLOAD_TRUNCATED_SUFFIX


def _full_tool_calls(rows, *, limit: int | None) -> list[ToolCallOut]:
    tool_calls: list[ToolCallOut] = []
    for tc in rows:
        input_text = str(tc.get("input") or "")
        output_text = str(tc.get("output") or "")
        input_truncated = limit is not None and len(input_text) > limit
        output_truncated = limit is not None and len(output_text) > limit
        tool_calls.append(
            ToolCallOut(
                id=int(tc["id"]),
                tool_name=str(tc["tool_name"]),
                input=input_text if limit is None else _history_tool_payload(input_text, limit),
                output=output_text if limit is None else _history_tool_payload(output_text, limit),
                status=str(tc["status"]),
                preview_only=input_truncated or output_truncated,
                has_full_input=not input_truncated,
                has_full_output=not output_truncated,
            )
        )
    return tool_calls


def _summary_tool_calls(db, *, message_id: int, limit: int) -> list[ToolCallOut]:
    rows = db.fetchall(
        """
        SELECT
            id,
            tool_name,
            status,
            CASE
                WHEN ? <= 0 THEN ''
                ELSE substr(COALESCE(input, ''), 1, ?)
            END AS input_preview,
            CASE
                WHEN ? <= 0 THEN ''
                ELSE substr(COALESCE(output, ''), 1, ?)
            END AS output_preview,
            length(COALESCE(input, '')) AS input_length,
            length(COALESCE(output, '')) AS output_length
        FROM tool_calls
        WHERE message_id = ?
        ORDER BY id
        """,
        (limit, limit, limit, limit, message_id),
    )
    return [
        ToolCallOut(
            id=int(row["id"]),
            tool_name=str(row["tool_name"]),
            input=_tool_call_preview(row["input_preview"], row["input_length"], limit),
            output=_tool_call_preview(row["output_preview"], row["output_length"], limit),
            status=str(row["status"]),
            preview_only=True,
            has_full_input=int(row["input_length"] or 0) > len(str(row["input_preview"] or "")),
            has_full_output=int(row["output_length"] or 0) > len(str(row["output_preview"] or "")),
        )
        for row in rows
    ]


def _completion_protocol_text() -> str:
    return "\n".join(
        [
            "## Completion Protocol",
            "- Use the same decision rule for every provider and model.",
            "- If more work is needed, call the next real tool instead of describing the tool you plan to use.",
            "- If the request can be answered without tools, answer directly and finish the turn.",
            "- Do not send a greeting, filler, or a progress-only update when tools are still available.",
            "- Do not claim that you already answered a fresh user question unless the persisted conversation history shows that exact answer.",
        ]
    )


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    limit: int = Query(100, ge=1, le=CONVERSATION_LIST_LIMIT_MAX),
    offset: int = Query(0, ge=0, le=10_000),
):
    conversations = list_conversation_metadata()
    return conversations[offset:offset + limit]


@router.post("/conversations", response_model=ConversationOut)
async def create_conversation(body: ConversationCreate):
    conv_id = str(uuid.uuid4())
    conversation = create_conversation_file(conv_id, body.title)
    publish_ui_event("conversation.changed", {"conversation_id": conv_id, "action": "created"})
    return conversation


@router.get("/conversations/{conv_id}/context-usage", response_model=ContextUsagePayload)
async def get_context_usage(conv_id: str):
    db = get_db()
    if db.get_conversation(conv_id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    tool_call_counter = getattr(db, "count_tool_calls_for_conversation", None)
    tool_call_count = int(tool_call_counter(conv_id)) if callable(tool_call_counter) else 0
    runtime_settings = build_runtime_namespace(settings, load_agent_settings(settings))
    runtime = get_runtime(runtime_settings)
    visible_tools = runtime.tool_registry.get_all_tools(visible_only=True)
    all_tools = runtime.tool_registry.get_all_tools(visible_only=False)
    visible_names = {str(tool.get("name", "")) for tool in visible_tools}
    hidden_tools = [tool for tool in all_tools if str(tool.get("name", "")) not in visible_names]

    skill_sections = build_skill_prompt_sections(
        runtime.skills,
        {str(tool.get("name", "")) for tool in visible_tools},
    )
    runtime_prompt_text = "\n\n".join(
        part
        for part in [
            skill_sections.get("runtime_rules", ""),
            skill_sections.get("browser_policy", ""),
            skill_sections.get("computer_use_policy", ""),
            build_identity_prompt(runtime_settings),
            _completion_protocol_text(),
        ]
        if part
    )
    skill_prompt_text = "\n\n".join(
        part
        for part in [
            skill_sections.get("capability_checklist", ""),
            skill_sections.get("skills_available", ""),
        ]
        if part
    )
    long_term_query = infer_context_query(runtime.memory, conv_id)
    long_term_context = runtime.memory.build_long_term_memory_context(long_term_query, mark_used=False)

    return build_context_usage_report(
        memory=runtime.memory,
        llm_client=runtime.llm_client,
        conversation_id=conv_id,
        runtime_prompt_text=runtime_prompt_text,
        skill_prompt_text=skill_prompt_text,
        long_term_context=long_term_context,
        visible_tools=visible_tools,
        hidden_tools=hidden_tools,
        tool_call_count=tool_call_count,
        limit=model_context_token_limit(runtime.llm_client),
        compaction_at=model_compaction_threshold(runtime.llm_client),
    )


@router.patch("/conversations/{conv_id}", response_model=ConversationOut)
async def rename_conversation(conv_id: str, body: ConversationRename):
    db = get_db()
    if db.get_conversation(conv_id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    db.update_conversation(conv_id, title=body.title.strip())
    conv = db.get_conversation(conv_id)
    publish_ui_event("conversation.changed", {"conversation_id": conv_id, "action": "renamed"})
    return conv


@router.delete("/conversations/{conv_id}", response_model=OkResponse)
async def delete_conversation(conv_id: str):
    memory_manager = peek_memory_manager()
    found = memory_manager.delete(conv_id) if memory_manager is not None else delete_conversation_file(conv_id)
    if not found:
        raise HTTPException(status_code=404, detail="Conversation not found")
    publish_ui_event("conversation.changed", {"conversation_id": conv_id, "action": "deleted"})
    return {"ok": True}


@router.get("/conversations/{conv_id}/messages", response_model=MessagesResponse)
async def get_conversation_messages(
    request: Request,
    conv_id: str,
    limit: int = Query(50, ge=1, le=MESSAGE_LIMIT_MAX),
    before_id: int | None = Query(None),
    tool_payload_limit: int | None = Query(None, ge=0, le=MESSAGE_TOOL_PAYLOAD_LIMIT_MAX),
    include_tool_calls: bool = Query(True),
    tool_call_mode: str | None = Query(
        None,
        pattern="^(none|summary|full)$",
    ),
):
    """Retrieve messages for a conversation with cursor-based pagination."""
    db = get_db()
    conv = db.get_conversation(conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    resolved_tool_call_mode = _tool_call_mode(
        include_tool_calls=include_tool_calls,
        tool_call_mode=tool_call_mode,
    )
    preview_tool_payload_limit = (
        HISTORY_TOOL_PAYLOAD_PREVIEW_CHARS
        if tool_payload_limit is None
        else tool_payload_limit
    )

    raw_messages = db.get_messages(conv_id, limit=limit + 1, before_id=before_id)
    has_more = len(raw_messages) > limit
    if has_more:
        raw_messages = raw_messages[1:]

    result = []
    for msg in raw_messages:
        message_id = int(msg["id"])
        # Fetch this message's full tool-call rows at most once and reuse them for
        # both the ToolCallOut list (full mode) and attachment collection, instead
        # of querying tool_calls twice for the same message.
        full_tool_call_rows = None
        if resolved_tool_call_mode == TOOL_CALL_MODE_FULL:
            full_tool_call_rows = db.get_tool_calls_for_message(message_id)
            tool_calls = _full_tool_calls(full_tool_call_rows, limit=None)
        elif resolved_tool_call_mode == TOOL_CALL_MODE_SUMMARY:
            tool_calls = _summary_tool_calls(db, message_id=message_id, limit=preview_tool_payload_limit)
        else:
            tool_calls = []
        control_session_id = request.state.control_session.session_id
        stored_attachments = _stored_response_attachments(
            msg.get("attachments_json"),
            control_session_id=control_session_id,
            conversation_id=conv_id,
        )
        attachments = stored_attachments
        if attachments is None:
            if full_tool_call_rows is None:
                full_tool_call_rows = db.get_tool_calls_for_message(message_id)
            attachment_tool_calls = [dict(tc) for tc in full_tool_call_rows]
            attachments = (
                collect_response_attachments(
                    content=msg["content"],
                    tool_calls=attachment_tool_calls,
                    control_session_id=control_session_id,
                    principal_id=control_session_id,
                    conversation_id=conv_id,
                )
                if attachment_tool_calls
                else collect_response_attachments(
                    content=msg["content"],
                    control_session_id=control_session_id,
                    principal_id=control_session_id,
                    conversation_id=conv_id,
                )
            )
        result.append(
            MessageOut(
                id=message_id,
                role=msg["role"],
                content=msg["content"],
                thinking=msg.get("thinking", ""),
                status=msg.get("status", "complete"),
                response_duration_ms=msg.get("response_duration_ms"),
                tool_calls=tool_calls,
                attachments=attachments,
                created_at=msg["created_at"],
            )
        )

    next_before_id = result[0].id if result else None
    return MessagesResponse(messages=result, has_more=has_more, next_before_id=next_before_id)


@router.get("/messages/{message_id}/tool-calls", response_model=list[ToolCallOut])
async def get_message_tool_calls(
    message_id: int,
    limit: int = Query(100, ge=1, le=MESSAGE_TOOL_CALL_LIMIT_MAX),
    offset: int = Query(0, ge=0, le=10_000),
    tool_payload_limit: int | None = Query(
        None,
        ge=0,
        le=MESSAGE_TOOL_PAYLOAD_LIMIT_MAX,
    ),
):
    db = get_db()
    message = db.fetchone("SELECT id FROM messages WHERE id = ?", (message_id,))
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found")
    rows = db.get_tool_calls_for_message(int(message["id"]))
    rows = rows[offset:offset + limit]
    return _full_tool_calls(rows, limit=tool_payload_limit)
