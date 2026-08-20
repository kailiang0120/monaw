import importlib.util
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.agent.runtime_paths import RUNTIME_DIR
from app.agent.sandbox.capabilities import get_sandbox_status
from app.agent.scheduler import get_scheduled_task_service
from app.agent.settings_store import build_runtime_namespace, load_agent_settings
from app.agent.skill_loader import available_skill_payload
from app.config import settings
from app.agent.observability.recorder import redact
from app.schemas import BrowserUseDiagnosticsOut, DiagnosticsSummaryPayload, MCPServerDiagnosticsOut
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
        "path": "",
        "exists": exists,
        "size_bytes": path.stat().st_size if exists and path.is_file() else 0,
    }


def _safe_mcp_entry(entry: dict) -> dict:
    # This route is authenticated and loopback-only. The local UI needs the
    # process and transport details to explain why a server is unhealthy, but
    # environment variables and HTTP headers may contain credentials.
    safe = dict(entry)
    safe["env"] = redact(entry.get("env", {}))
    safe["headers"] = redact(entry.get("headers", {}))
    return safe


def _safe_browser_diagnostics(diagnostics: dict) -> dict:
    safe = redact(diagnostics)
    for key in (
        "system_cdp_url",
        "managed_profile_dir",
        "downloads_dir",
        "screenshots_dir",
        "traces_dir",
        "system_profile_directory",
        "chrome_executable",
    ):
        safe[key] = ""
    profiles = safe.get("available_system_profiles")
    if isinstance(profiles, list):
        for profile in profiles:
            if isinstance(profile, dict):
                profile["directory"] = ""
    return safe


@router.get("/diagnostics/summary", response_model=DiagnosticsSummaryPayload)
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
    mcp_startup = dict(getattr(request.app.state, "mcp_status", {}) or {})
    browser_diagnostics = await get_browser_use_diagnostics(runtime_namespace)
    sandbox_status = get_sandbox_status(runtime_settings.sandbox)
    db_path = RUNTIME_DIR / "agent.db"

    return {
        "backend": {
            "name": "Monaw Agent API",
            "version": "1.0.0",
            "port": settings.port,
            "runtime_dir": "",
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
            "startup_error": str(mcp_startup.get("startup_error", "") or ""),
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


@router.get("/diagnostics/mcp", response_model=list[MCPServerDiagnosticsOut])
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
        payload.append(_safe_mcp_entry(entry))
        seen.add(name)

    for name, entry in diagnostics.items():
        if name in seen:
            continue
        entry["enabled"] = False
        entry["feature_enabled"] = feature_enabled
        entry["feature_available"] = feature_available
        entry["feature_unavailable_reason"] = feature_unavailable_reason
        payload.append(_safe_mcp_entry(entry))

    return payload


@router.post("/diagnostics/mcp/{name}/reconnect", response_model=MCPServerDiagnosticsOut)
async def reconnect_mcp(name: str):
    runtime_settings = load_agent_settings(settings)
    if not runtime_settings.mcp.enabled:
        raise HTTPException(status_code=409, detail="MCP feature is disabled")
    status = reconnect_mcp_server(name)
    if status is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return _safe_mcp_entry(status)


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
    return _safe_browser_diagnostics(diagnostics)


@router.post("/diagnostics/browser-use/reset", response_model=BrowserUseDiagnosticsOut)
async def reset_browser_use_status():
    await reset_browser_use_runtime(clear_managed_session=True)
    return await get_browser_use_status()
