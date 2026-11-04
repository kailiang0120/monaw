import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.agent.job_manager import cancel_job, create_job, get_job, list_jobs, run_job
from app.agent.response_attachments import runtime_attachment_from_ref
from app.agent.settings_store import build_runtime_namespace, load_agent_settings
from app.agent.steering import (
    SteeringInbox, SteeringMessage, get_steering, register_steering, running_turns, unregister_steering,
)
from app.agent.ui_events import publish_ui_event
from app.config import settings
from app.debug_ndjson import dbg_log
from app.schemas import (
    ChatJobCreateResponse, ChatJobOut, ChatRequest, ChatRunOut, ChatSteerRequest, ChatSteerResponse, ChatStopRequest,
    OkResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _message_with_attachments(req: ChatRequest) -> str:
    message = (req.message or "").strip()
    if not req.attachments:
        return message

    lines = [
        message,
        "",
        "Attached files are available for tool use by opaque handle:",
    ]
    for attachment in req.attachments:
        details = [attachment.name]
        if attachment.mime_type:
            details.append(attachment.mime_type)
        if attachment.size:
            details.append(f"{attachment.size} bytes")
        lines.append(f"- {' | '.join(details)}")
        lines.append(f"  handle: attachment://{attachment.id}")
    lines.append("")
    lines.append(
        "Use the attachment handle exactly as provided with filesystem tools when reading, analyzing, editing, or converting the attachment."
    )
    return "\n".join(lines).strip()


def _runtime_attachments(req: ChatRequest, *, control_session_id: str, conversation_id: str) -> list[dict]:
    attachments: list[dict] = []
    for attachment in req.attachments:
        runtime_attachment = runtime_attachment_from_ref(
            attachment,
            control_session_id=control_session_id,
            principal_id=control_session_id,
            conversation_id=conversation_id,
        )
        if runtime_attachment is None:
            raise HTTPException(status_code=404, detail=f"Attachment not found: {attachment.id}")
        attachments.append(runtime_attachment)
    return attachments


def _prepare_conversation_workspace(req: ChatRequest, conv_id: str) -> None:
    if req.workspace_dir is not None:
        from app.agent.conversation_workspace import validate_workspace_dir
        from app.agent.database import get_db
        from app.agent.turn_text import conversation_title_from_prompt

        try:
            workspace_dir = validate_workspace_dir(req.workspace_dir)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        db = get_db()
        existing = db.get_conversation(conv_id)
        if existing is None:
            db.create_conversation(conv_id, conversation_title_from_prompt(req.message), workspace_dir)
        elif existing.get("workspace_dir", "") != workspace_dir:
            raise HTTPException(status_code=409, detail="Change the working folder before starting a task.")


@router.post("/chat")
async def chat(req: ChatRequest, request: Request):
    conv_id = req.conversation_id or str(uuid.uuid4())
    message = _message_with_attachments(req)
    control_session_id = request.state.control_session.session_id
    owner_principal_id = request.state.control_session.profile_id or control_session_id
    runtime_attachments = _runtime_attachments(
        req,
        control_session_id=control_session_id,
        conversation_id=conv_id,
    )
    _prepare_conversation_workspace(req, conv_id)

    inbox = SteeringInbox(
        conv_id, control_session_id, **({"run_id": req.run_id} if req.run_id else {}),
        display_message=req.message, public_attachments=[attachment.model_dump() for attachment in req.attachments],
    )
    try:
        register_steering(inbox)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # Lets the sidebar mark the chat as running.
    publish_ui_event("conversation.changed", {"conversation_id": conv_id, "action": "run_started"})

    async def run_turn() -> None:
        # The turn runs apart from the HTTP response: closing the window or
        # switching chats only stops the viewing, and POST /chat/stop ends it.
        try:
            inbox.stream.publish("turn_started", {"run_id": inbox.run_id, "conversation_id": conv_id})
            merged = build_runtime_namespace(settings, load_agent_settings(settings))
            from app.agent.runtime import run_agent_stream

            async for event_dict in run_agent_stream(
                message=message,
                conversation_id=conv_id,
                settings=merged,
                attachments=runtime_attachments,
                control_session_id=control_session_id,
                execution_source="desktop",
                principal_id=owner_principal_id,
                permission_profile_id="desktop-current",
                interactive=True,
                steering=inbox,
                durable_run_id=inbox.run_id,
            ):
                event_name = event_dict.get("event", "message")
                if event_name == "done":
                    publish_ui_event(
                        "conversation.changed",
                        {"conversation_id": conv_id, "action": "messages_changed"},
                    )
                    publish_ui_event("usage.changed", {"conversation_id": conv_id})
                    publish_ui_event("observability.changed", {"conversation_id": conv_id})
                inbox.stream.publish(event_name, event_dict.get("data", {}))
        except asyncio.CancelledError:
            # Stopped on request: the turn saved its partial result. Tell
            # other views the chat changed, then finish cancelling.
            publish_ui_event("conversation.changed", {"conversation_id": conv_id, "action": "messages_changed"})
            raise
        except Exception as e:
            dbg_log(
                "routes.py:chat.stream",
                "sse_stream_exception",
                hypothesis_id="H1",
                data={"error_type": type(e).__name__, "error_msg": str(e)[:300]},
            )
            logger.exception("Chat turn failed")
        finally:
            unregister_steering(inbox)
            inbox.stream.close()
            publish_ui_event("conversation.changed", {"conversation_id": conv_id, "action": "run_finished"})

    inbox.task = asyncio.create_task(run_turn())
    events = inbox.stream.subscribe()

    async def stream():
        dbg_log(
            "routes.py:chat.stream",
            "sse_stream_start",
            hypothesis_id="H1",
            data={"conv_id": conv_id, "msg_len": len(message or ""), "attachments": len(req.attachments)},
        )
        try:
            while (chunk := await events.get()) is not None:
                yield chunk
        finally:
            inbox.stream.unsubscribe(events)
            dbg_log("routes.py:chat.stream", "sse_stream_generator_finally", hypothesis_id="H1", data={})

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/chat/runs", response_model=list[ChatRunOut])
async def list_chat_runs(request: Request):
    """Turns this session started that are still running, so a reopened chat can follow them."""
    return [
        {
            "run_id": inbox.run_id, "conversation_id": inbox.conversation_id, "message": inbox.display_message,
            "attachments": inbox.public_attachments, "started_at": inbox.started_at,
            "continuation": inbox.continuation,
        }
        for inbox in running_turns(request.state.control_session.session_id)
    ]


@router.get("/chat/runs/{run_id}/events")
async def follow_chat_run(run_id: str, request: Request):
    """Replay a running turn's events from its start, then stream the rest as they happen."""
    inbox = get_steering(run_id, request.state.control_session.session_id)
    if inbox is None:
        raise HTTPException(status_code=404, detail="This turn is no longer running.")
    events = inbox.stream.subscribe()

    async def stream():
        try:
            while (chunk := await events.get()) is not None:
                yield chunk
        finally:
            inbox.stream.unsubscribe(events)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/chat/stop", response_model=OkResponse)
async def stop_chat(body: ChatStopRequest, request: Request):
    """End a running turn; only the session that started it may stop it."""
    inbox = get_steering(body.run_id, request.state.control_session.session_id)
    task = inbox.task if inbox is not None else None
    if task is None or task.done():
        return {"ok": False}
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=10)
    except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
        pass
    return {"ok": True}


@router.post("/chat/steer", response_model=ChatSteerResponse)
async def steer_chat(body: ChatSteerRequest, request: Request):
    session_id = request.state.control_session.session_id
    inbox = get_steering(body.run_id, session_id)
    if inbox is None:
        raise HTTPException(status_code=409, detail="This turn is no longer running. Send the message as a new turn.")
    req = ChatRequest(message=body.message, conversation_id=inbox.conversation_id, attachments=body.attachments)
    attachments = _runtime_attachments(req, control_session_id=session_id, conversation_id=inbox.conversation_id)
    try:
        item = inbox.enqueue(SteeringMessage(
            body.message_id, _message_with_attachments(req), attachments,
            display_message=body.message,
            public_attachments=[attachment.model_dump() for attachment in body.attachments],
        ))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OverflowError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {"message_id": item.id, "conversation_id": inbox.conversation_id, "status": "applied" if item.applied else "queued"}


@router.post("/chat/jobs", response_model=ChatJobCreateResponse)
async def create_chat_job(req: ChatRequest, request: Request):
    """Start a detached background job. Returns job_id immediately."""
    conv_id = req.conversation_id or str(uuid.uuid4())
    message = _message_with_attachments(req)
    merged = build_runtime_namespace(settings, load_agent_settings(settings))
    control_session_id = request.state.control_session.session_id
    owner_principal_id = request.state.control_session.profile_id or control_session_id
    runtime_attachments = _runtime_attachments(
        req,
        control_session_id=control_session_id,
        conversation_id=conv_id,
    )
    _prepare_conversation_workspace(req, conv_id)
    job = create_job(conv_id)

    async def _start():
        await run_job(
            job,
            message,
            merged,
            attachments=runtime_attachments,
            control_session_id=control_session_id,
            execution_source="desktop_job",
            principal_id=owner_principal_id,
            permission_profile_id="desktop-current",
            interactive=True,
        )

    asyncio.create_task(_start())
    publish_ui_event("conversation.changed", {"conversation_id": conv_id, "action": "job_created"})
    return {"job_id": job.job_id, "conversation_id": conv_id}


@router.get("/chat/jobs", response_model=list[ChatJobOut])
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
            "dropped_subscriber_events": j.dropped_subscriber_events,
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


@router.post("/chat/jobs/{job_id}/cancel", response_model=OkResponse)
async def cancel_chat_job(job_id: str):
    """Cancel a running job."""
    ok = await cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found or already finished")
    job = get_job(job_id)
    publish_ui_event(
        "conversation.changed",
        {"conversation_id": job.conversation_id if job is not None else "", "action": "job_cancelled"},
    )
    return {"ok": True}
