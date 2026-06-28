import json

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

from app.agent.ui_events import subscribe_ui_events

router = APIRouter()


@router.get("/events")
async def stream_ui_events(
    request: Request,
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
):
    parsed_last_id: int | None = None
    if last_event_id:
        try:
            parsed_last_id = int(last_event_id)
        except (TypeError, ValueError):
            parsed_last_id = None

    async def stream():
        async for event in subscribe_ui_events(parsed_last_id):
            if await request.is_disconnected():
                break
            event_data = json.dumps(event.data, separators=(",", ":"))
            id_line = f"id: {event.seq}\n" if event.seq > 0 else ""
            yield f"{id_line}event: {event.event}\ndata: {event_data}\n\n".encode()

    return StreamingResponse(stream(), media_type="text/event-stream")
