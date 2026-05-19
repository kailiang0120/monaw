import importlib.util
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.agent.runtime_paths import RUNTIME_DIR
from app.agent.sandbox.capabilities import get_sandbox_status
from app.agent.scheduler import get_scheduled_task_service
from app.agent.settings_store import build_runtime_namespace, load_agent_settings
from app.agent.skill_loader import available_skill_payload
from app.agent.observability.trajectory import get_trajectory_logger
from app.agent.runtime import run_agent_stream
from app.config import settings
from app.schemas import BrowserUseDiagnosticsOut
from app.skills.browser_use.manager import (
    ensure_browser_use_runtime_dirs,
    get_browser_use_diagnostics,
    reset_browser_use_runtime,
)
from app.skills.mcp_bridge.connection import get_mcp_runtime_diagnostics, reconnect_mcp_server

router = APIRouter()


def _runtime_file_status(path: Path) -> dict:
    exists = path.exists()
    return {
        "path": str(path),
        "exists": exists,
        "size_bytes": path.stat().st_size if exists and path.is_file() else 0,
    }


def _first_human_message(payload: dict) -> str:
    entries = payload.get("conversations") if isinstance(payload.get("conversations"), list) else []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("from") == "human":
            return str(entry.get("value") or "").strip()
    return ""


@router.get("/diagnostics/summary")
async def get_diagnostics_summary(request: Request):
    runtime_settings = load_agent_settings(settings)
    runtime_namespace = build_runtime_namespace(settings, runtime_settings)
    scheduler_service = get_scheduled_task_service()
    scheduler_state = dict(getattr(request.app.state, "scheduler_status", {}) or {})
    if scheduler_service is not None:
        scheduler_state.update(scheduler_service.status())
    else:
        scheduler_state.setdefault("running", False)

    telegram_state = dict(getattr(request.app.state, "telegram_status", {}) or {})
    try:
        from app.integrations.telegram.service import telegram_bot_service

        telegram_state["running"] = telegram_bot_service.running
    except Exception:
        telegram_state.setdefault("running", False)
    telegram_state.setdefault("configured", bool(settings.telegram_bot_token))
    telegram_state.setdefault("startup_error", "")

    mcp_diagnostics = get_mcp_runtime_diagnostics()
    browser_diagnostics = await get_browser_use_diagnostics(runtime_namespace)
    sandbox_status = get_sandbox_status(runtime_settings.sandbox)
    db_path = RUNTIME_DIR / "agent.db"

    return {
        "backend": {
            "name": "Monaw Agent API",
            "version": "1.0.0",
            "port": settings.port,
            "runtime_dir": str(RUNTIME_DIR),
            "database": _runtime_file_status(db_path),
        },
        "scheduler": scheduler_state,
        "telegram": telegram_state,
        "mcp": {
            "enabled": bool(runtime_settings.mcp.enabled),
            "configured_servers": len(runtime_settings.mcp.servers),
            "runtime_servers": len(mcp_diagnostics),
            "connected_servers": sum(1 for item in mcp_diagnostics if item.get("connected")),
            "unhealthy_servers": sum(1 for item in mcp_diagnostics if item.get("unhealthy_reason")),
        },
        "browser": {
            "available": bool(browser_diagnostics.get("available")),
            "session_active": bool(browser_diagnostics.get("session_active")),
            "preferred_mode": browser_diagnostics.get("preferred_mode", ""),
            "current_mode": browser_diagnostics.get("current_mode", ""),
            "last_error": browser_diagnostics.get("last_error", ""),
        },
        "sandbox": sandbox_status,
    }


@router.get("/diagnostics/mcp")
async def get_mcp_diagnostics():
    runtime_settings = load_agent_settings(settings)
    feature_enabled = bool(runtime_settings.mcp.enabled)
    feature_available = importlib.util.find_spec("mcp") is not None
    feature_unavailable_reason = "" if feature_available else "missing_backend_dependency"
    diagnostics = {
        item["name"]: dict(item)
        for item in get_mcp_runtime_diagnostics()
    }

    payload: list[dict] = []
    seen: set[str] = set()
    for server in runtime_settings.mcp.servers:
        if isinstance(server, dict):
            name = str(server.get("name", "")).strip()
            enabled = bool(server.get("enabled", False))
            description = str(server.get("description", "") or "")
            transport = str(server.get("transport", "stdio") or "stdio")
            command = str(server.get("command", "") or "")
            args = list(server.get("args", []) or [])
            cwd = str(server.get("cwd", "") or "")
            url = str(server.get("url", "") or "")
        else:
            name = server.name
            enabled = server.enabled
            description = server.description
            transport = server.transport
            command = server.command
            args = list(server.args)
            cwd = server.cwd
            url = server.url
        entry = diagnostics.get(
            name,
            {
                "name": name,
                "transport": transport,
                "connected": False,
                "state": "stopped",
                "tool_count": 0,
                "last_error": None,
                "unhealthy_reason": None,
                "description": description,
                "command": command,
                "args": args,
                "cwd": cwd,
                "url": url,
                "resolved_executable": "",
                "startup_phase": "idle",
                "pid": None,
                "started_at": None,
                "connected_at": None,
                "disconnected_at": None,
                "stderr_tail": "",
                "last_call_started_at": None,
                "last_call_duration_ms": None,
                "failed_call_count": 0,
                "remote_tool_names": [],
                "reflected_tool_names": [],
            },
        )
        entry["enabled"] = enabled
        entry["transport"] = entry.get("transport") or transport
        entry["command"] = entry.get("command") or command
        entry["args"] = entry.get("args") or args
        entry["cwd"] = entry.get("cwd") or cwd
        entry["url"] = entry.get("url") or url
        entry["feature_enabled"] = feature_enabled
        entry["feature_available"] = feature_available
        entry["feature_unavailable_reason"] = feature_unavailable_reason
        entry["skill_enabled"] = feature_enabled
        entry["skill_available"] = feature_available
        entry["skill_unavailable_reason"] = feature_unavailable_reason
        entry["login_capable"] = "chrome" in name.lower() or "devtools" in description.lower()
        payload.append(entry)
        seen.add(name)

    for name, entry in diagnostics.items():
        if name in seen:
            continue
        entry["enabled"] = False
        entry["feature_enabled"] = feature_enabled
        entry["feature_available"] = feature_available
        entry["feature_unavailable_reason"] = feature_unavailable_reason
        entry["skill_enabled"] = feature_enabled
        entry["skill_available"] = feature_available
        entry["skill_unavailable_reason"] = feature_unavailable_reason
        description = str(entry.get("description", "") or "")
        entry["login_capable"] = "chrome" in name.lower() or "devtools" in description.lower()
        payload.append(entry)

    return payload


@router.post("/diagnostics/mcp/{name}/reconnect")
async def reconnect_mcp(name: str):
    runtime_settings = load_agent_settings(settings)
    if not runtime_settings.mcp.enabled:
        raise HTTPException(status_code=409, detail="MCP feature is disabled")
    status = reconnect_mcp_server(name)
    if status is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return status


@router.get("/diagnostics/browser-use", response_model=BrowserUseDiagnosticsOut)
async def get_browser_use_status():
    ensure_browser_use_runtime_dirs()
    runtime_settings = load_agent_settings(settings)
    runtime_namespace = build_runtime_namespace(settings, runtime_settings)
    available_skills = {
        item["name"]: item
        for item in available_skill_payload(runtime_namespace)
    }
    browser_skill = available_skills.get("browser-use", {})
    diagnostics = await get_browser_use_diagnostics(runtime_namespace)
    diagnostics["skill_enabled"] = bool(runtime_settings.tools.skills.get("browser-use", False))
    diagnostics["skill_available"] = bool(browser_skill.get("available", False))
    diagnostics["skill_unavailable_reason"] = str(browser_skill.get("unavailable_reason", "") or "")
    return diagnostics


@router.post("/diagnostics/browser-use/reset", response_model=BrowserUseDiagnosticsOut)
async def reset_browser_use_status():
    await reset_browser_use_runtime(clear_managed_session=True)
    return await get_browser_use_status()


@router.get("/diagnostics/evaluations")
async def list_evaluation_runs(limit: int = 50):
    return get_trajectory_logger().list_runs(limit=limit)


@router.get("/diagnostics/evaluations/{run_id}")
async def get_evaluation_run(run_id: str):
    payload = get_trajectory_logger().get_run(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Evaluation run not found")
    return payload


@router.post("/diagnostics/evaluations/{run_id}/replay")
async def replay_evaluation_run(run_id: str):
    payload = get_trajectory_logger().get_run(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Evaluation run not found")
    message = _first_human_message(payload)
    if not message:
        raise HTTPException(status_code=400, detail="Evaluation run has no user message to replay")

    import uuid

    conversation_id = f"replay_{uuid.uuid4().hex[:20]}"
    runtime_settings = build_runtime_namespace(settings, load_agent_settings(settings))
    summary = ""
    status = "complete"
    errors: list[str] = []
    async for event in run_agent_stream(
        message=message,
        conversation_id=conversation_id,
        settings=runtime_settings,
        attachments=None,
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
    return {
        "source_run_id": run_id,
        "conversation_id": conversation_id,
        "status": status,
        "summary": summary,
        "errors": errors,
    }
