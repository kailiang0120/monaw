import uuid

from fastapi import APIRouter, HTTPException, Request

from app.agent.observability.recorder import get_observability_recorder
from app.agent.settings_store import build_runtime_namespace, load_agent_settings
from app.config import settings
from app.security.support_mode import (
    disable_support_mode,
    enable_support_mode,
    support_mode_status,
)

router = APIRouter()


@router.get("/observability/summary")
async def observability_summary():
    return get_observability_recorder().summary()


@router.get("/observability/runs")
async def observability_runs(
    status: str = "",
    source: str = "",
    model: str = "",
    q: str = "",
    limit: int = 100,
):
    return get_observability_recorder().list_runs(
        status=status,
        source=source,
        model=model,
        q=q,
        limit=limit,
    )


@router.get("/observability/runs/{run_id}")
async def observability_run_detail(run_id: str):
    payload = get_observability_recorder().get_run(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return payload


@router.get("/observability/errors")
async def observability_errors(level: str = "", q: str = "", limit: int = 100):
    return get_observability_recorder().list_errors(level=level, q=q, limit=limit)


def _support_mode_payload(request: Request) -> dict:
    status = support_mode_status(request.state.control_session.session_id)
    return {
        "enabled": status.enabled,
        "expires_at_epoch": status.expires_at_epoch,
        "remaining_seconds": status.remaining_seconds,
    }


def _require_support_mode(request: Request) -> None:
    if not support_mode_status(request.state.control_session.session_id).enabled:
        raise HTTPException(status_code=403, detail="Support mode is required for detailed diagnostics")


@router.get("/observability/support-mode")
async def observability_support_mode_status(request: Request):
    return _support_mode_payload(request)


@router.post("/observability/support-mode")
async def observability_enable_support_mode(request: Request, duration_seconds: int = 300):
    status = enable_support_mode(request.state.control_session.session_id, duration_seconds)
    return {
        "enabled": status.enabled,
        "expires_at_epoch": status.expires_at_epoch,
        "remaining_seconds": status.remaining_seconds,
    }


@router.delete("/observability/support-mode")
async def observability_disable_support_mode(request: Request):
    status = disable_support_mode(request.state.control_session.session_id)
    return {
        "enabled": status.enabled,
        "expires_at_epoch": status.expires_at_epoch,
        "remaining_seconds": status.remaining_seconds,
    }


@router.get("/observability/logs/backend")
async def observability_backend_log(request: Request, tail: int = 400):
    _require_support_mode(request)
    return get_observability_recorder().read_backend_log_tail(tail=tail)


@router.post("/observability/runs/{run_id}/replay")
async def replay_observability_run(run_id: str):
    recorder = get_observability_recorder()
    source_run = recorder.get_run(run_id, include_sensitive=True)
    if source_run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    message = str(source_run.get("user_message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Run has no user message to replay")

    conversation_id = f"replay_{uuid.uuid4().hex[:20]}"
    runtime_settings = build_runtime_namespace(settings, load_agent_settings(settings))
    from app.agent.runtime import run_agent_stream

    summary = ""
    status = "complete"
    errors: list[str] = []
    async for event in run_agent_stream(
        message=message,
        conversation_id=conversation_id,
        settings=runtime_settings,
        attachments=None,
        control_session_id=request.state.control_session.session_id,
        execution_source="replay",
        principal_id=f"replay:{request.state.control_session.session_id}",
        permission_profile_id="replay-restricted",
        interactive=False,
    ):
        event_name = str(event.get("event") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if event_name == "done":
            summary = str(data.get("summary") or "")
            status = str(data.get("status") or status)
            if data.get("incomplete") and data.get("reason_code"):
                errors.append(str(data.get("reason_code")))
        elif event_name == "error":
            errors.append(str(data.get("message") or data.get("code") or "error"))

    replay_run = recorder.latest_run_for_conversation(conversation_id) or {}
    replay_run_id = str(replay_run.get("run_id") or "")
    replay_detail = recorder.get_run(replay_run_id) if replay_run_id else None
    if replay_detail:
        replay_run = replay_detail

    source_status = str(source_run.get("status") or "")
    replay_status = str(replay_run.get("status") or status)
    source_tools = list(source_run.get("tool_sequence") or [])
    replay_tools = list(replay_run.get("tool_sequence") or [])
    tool_sequence_diff = "same" if source_tools == replay_tools else "different"
    source_failure = str(source_run.get("failure_reason") or "")
    replay_failure = str(replay_run.get("failure_reason") or "")
    failure_reason_diff = "same" if source_failure == replay_failure else f"{source_failure}->{replay_failure}"
    comparison = recorder.record_replay(
        source_run_id=run_id,
        replay_run_id=replay_run_id,
        conversation_id=conversation_id,
        status_change=f"{source_status}->{replay_status}",
        duration_delta_ms=int(replay_run.get("duration_ms") or 0) - int(source_run.get("duration_ms") or 0),
        token_delta=int(replay_run.get("total_tokens") or 0) - int(source_run.get("total_tokens") or 0),
        tool_sequence_diff=tool_sequence_diff,
        failure_reason_diff=failure_reason_diff,
        metadata={
            "source_tool_sequence": source_tools,
            "replay_tool_sequence": replay_tools,
            "errors": errors,
        },
    )
    return {
        "source_run_id": run_id,
        "replay_run_id": replay_run_id,
        "conversation_id": conversation_id,
        "status": replay_status,
        "summary": summary,
        "errors": errors,
        "comparison": comparison,
    }


@router.post("/observability/runs/{run_id}/export-debug-bundle")
async def export_observability_debug_bundle(run_id: str, request: Request):
    _require_support_mode(request)
    try:
        return get_observability_recorder().export_debug_bundle(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Run not found") from None
