import json
import uuid

from fastapi import APIRouter, HTTPException, Query

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
)
from app.agent.response_attachments import collect_response_attachments
from app.agent.runtime import build_identity_prompt, get_runtime
from app.agent.settings_store import build_runtime_namespace, load_agent_settings
from app.agent.skill_prompt import build_skill_prompt_sections
from app.config import settings
from app.schemas import (
    ConversationCreate,
    ConversationOut,
    ConversationRename,
    ContextUsagePayload,
    MessageOut,
    MessagesResponse,
    ToolCallOut,
)

router = APIRouter()


def _stored_response_attachments(raw_value: object) -> list[dict] | None:
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    try:
        payload = json.loads(raw_value)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, list):
        return None
    return [item for item in payload if isinstance(item, dict)]


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
async def list_conversations():
    runtime_settings = build_runtime_namespace(settings, load_agent_settings(settings))
    runtime = get_runtime(runtime_settings)
    return runtime.memory.list_conversations_metadata()


@router.post("/conversations", response_model=ConversationOut)
async def create_conversation(body: ConversationCreate):
    conv_id = str(uuid.uuid4())
    return create_conversation_file(conv_id, body.title)


@router.get("/conversations/{conv_id}/context-usage", response_model=ContextUsagePayload)
async def get_context_usage(conv_id: str):
    db = get_db()
    if db.get_conversation(conv_id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
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
    long_term_context = runtime.memory.build_long_term_memory_context(long_term_query)

    return build_context_usage_report(
        memory=runtime.memory,
        llm_client=runtime.llm_client,
        conversation_id=conv_id,
        runtime_prompt_text=runtime_prompt_text,
        skill_prompt_text=skill_prompt_text,
        long_term_context=long_term_context,
        visible_tools=visible_tools,
        hidden_tools=hidden_tools,
        tool_call_count=db.count_tool_calls_for_conversation(conv_id),
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
    return conv


@router.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    runtime_settings = build_runtime_namespace(settings, load_agent_settings(settings))
    runtime = get_runtime(runtime_settings)
    found_memory = runtime.memory.delete(conv_id)
    found_file = delete_conversation_file(conv_id)
    if not found_memory and not found_file:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"ok": True}


@router.get("/conversations/{conv_id}/messages", response_model=MessagesResponse)
async def get_conversation_messages(
    conv_id: str,
    limit: int = Query(50, ge=1, le=200),
    before_id: int | None = Query(None),
):
    """Retrieve messages for a conversation with cursor-based pagination."""
    db = get_db()
    conv = db.get_conversation(conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    raw_messages = db.get_messages(conv_id, limit=limit + 1, before_id=before_id)
    has_more = len(raw_messages) > limit
    if has_more:
        raw_messages = raw_messages[1:]

    result = []
    for msg in raw_messages:
        tc_rows = db.fetchall(
            "SELECT id, tool_name, input, output, status FROM tool_calls WHERE message_id = ? ORDER BY id",
            (msg["id"],),
        )
        tool_calls = [
            ToolCallOut(
                id=tc["id"],
                tool_name=tc["tool_name"],
                input=tc["input"],
                output=tc["output"],
                status=tc["status"],
            )
            for tc in tc_rows
        ]
        stored_attachments = _stored_response_attachments(msg.get("attachments_json"))
        result.append(
            MessageOut(
                id=msg["id"],
                role=msg["role"],
                content=msg["content"],
                thinking=msg.get("thinking", ""),
                status=msg.get("status", "complete"),
                response_duration_ms=msg.get("response_duration_ms"),
                tool_calls=tool_calls,
                attachments=stored_attachments
                if stored_attachments is not None
                else collect_response_attachments(
                    content=msg["content"],
                    tool_calls=[dict(tc) for tc in tc_rows],
                ),
                created_at=msg["created_at"],
            )
        )

    return MessagesResponse(messages=result, has_more=has_more)
