import asyncio
import json
import uuid

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse

from app.agent.job_manager import cancel_job, create_job, get_job, list_jobs, run_job
from app.agent.settings_store import build_runtime_namespace, load_agent_settings
from app.config import settings
from app.debug_ndjson import dbg_log
from app.schemas import ChatRequest

router = APIRouter()


def _message_with_attachments(req: ChatRequest) -> str:
    message = (req.message or "").strip()
    if not req.attachments:
        return message

    lines = [
        message,
        "",
        "Attached files are saved on this machine and are available for tool use:",
    ]
    for attachment in req.attachments:
        details = [attachment.name]
        if attachment.mime_type:
            details.append(attachment.mime_type)
        if attachment.size:
            details.append(f"{attachment.size} bytes")
        lines.append(f"- {' | '.join(details)}")
        lines.append(f"  path: {attachment.path}")
    lines.append("")
    lines.append(
        "Use the file path exactly as provided when reading, analyzing, editing, or converting the attachment."
    )
    return "\n".join(lines).strip()


@router.post("/chat")
async def chat(req: ChatRequest):
    conv_id = req.conversation_id or str(uuid.uuid4())
    message = _message_with_attachments(req)

    async def stream():
        dbg_log(
            "routes.py:chat.stream",
            "sse_stream_start",
            hypothesis_id="H1",
            data={"conv_id": conv_id, "msg_len": len(message or ""), "attachments": len(req.attachments)},
        )

        merged = build_runtime_namespace(settings, load_agent_settings(settings))
        from app.agent.runtime import run_agent_stream

        try:
            async for event_dict in run_agent_stream(
                message=message,
                conversation_id=conv_id,
                settings=merged,
                attachments=[attachment.model_dump() for attachment in req.attachments],
            ):
                event_name = event_dict.get("event", "message")
                event_data = json.dumps(event_dict.get("data", {}))
                yield f"event: {event_name}\ndata: {event_data}\n\n".encode()
        except Exception as e:
            dbg_log(
                "routes.py:chat.stream",
                "sse_stream_exception",
                hypothesis_id="H1",
                data={"error_type": type(e).__name__, "error_msg": str(e)[:300]},
            )
            raise
        finally:
            dbg_log("routes.py:chat.stream", "sse_stream_generator_finally", hypothesis_id="H1", data={})

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/chat/jobs")
async def create_chat_job(req: ChatRequest):
    """Start a detached background job. Returns job_id immediately."""
    conv_id = req.conversation_id or str(uuid.uuid4())
    message = _message_with_attachments(req)
    job = create_job(conv_id)
    merged = build_runtime_namespace(settings, load_agent_settings(settings))

    async def _start():
        await run_job(job, message, merged)

    asyncio.create_task(_start())
    return {"job_id": job.job_id, "conversation_id": conv_id}


@router.get("/chat/jobs")
async def list_chat_jobs():
    """List all known jobs and their statuses."""
    return [
        {
            "job_id": j.job_id,
            "conversation_id": j.conversation_id,
            "status": j.status,
            "started_at": j.started_at,
            "workflow_engine": j.workflow_engine,
            "active_graph_node": j.active_graph_node,
            "checkpoint_status": j.checkpoint_status,
            "last_resume_reason": j.last_resume_reason,
        }
        for j in list_jobs()
    ]


@router.get("/chat/jobs/{job_id}/stream")
async def stream_chat_job(
    job_id: str,
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
):
    """SSE stream for a job. Supports Last-Event-ID resume."""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    parsed_last_id: int | None = None
    if last_event_id is not None:
        try:
            parsed_last_id = int(last_event_id)
        except (ValueError, TypeError):
            parsed_last_id = None

    async def _stream():
        async for seq, event in job.subscribe(last_event_id=parsed_last_id):
            event_name = event.get("event", "message")
            event_data = json.dumps(event.get("data", {}))
            id_line = f"id: {seq}\n" if seq > 0 else ""
            yield f"{id_line}event: {event_name}\ndata: {event_data}\n\n".encode()

    return StreamingResponse(_stream(), media_type="text/event-stream")


@router.post("/chat/jobs/{job_id}/cancel")
async def cancel_chat_job(job_id: str):
    """Cancel a running job."""
    ok = await cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found or already finished")
    return {"ok": True}
