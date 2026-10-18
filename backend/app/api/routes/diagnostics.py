import asyncio
import importlib.util
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException, Request

from app.agent.runtime_paths import RUNTIME_DIR
from app.agent.sandbox.capabilities import get_sandbox_status
from app.agent.scheduler import get_scheduled_task_service
from app.agent.settings_store import build_runtime_namespace, load_agent_settings
from app.agent.skill_loader import available_skill_payload
from app.config import settings
from app.agent.observability.recorder import redact
from app.schemas import BrowserUseDiagnosticsOut, DiagnosticsSummaryPayload, MCPServerDiagnosticsOut
from app.skills.browser_use.chrome_launch import (
    find_chrome_executable,
    last_used_chrome_profile,
    list_local_chrome_profiles,
)
from app.skills.mcp_bridge.builtin import CHROME_DEVTOOLS_SERVER, is_builtin_server
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
    safe["command"] = _redact_mcp_text(entry.get("command", ""))
    safe["args"] = _redact_mcp_args(entry.get("args", []))
    safe["url"] = _redact_mcp_url(entry.get("url", ""))
    return safe


_SECRET_NAME_RE = re.compile(r"(?:api[-_]?key|token|secret|password|authorization|bearer|credential)", re.I)
_SECRET_ARG_RE = re.compile(r"^((?:--?|/)[^=:\s]*(?:api[-_]?key|token|secret|password|authorization|auth|credential)[^=:\s]*)(?:=|:)(.*)$", re.I)


def _redact_mcp_url(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
        query = [
            (key, "<redacted>" if _SECRET_NAME_RE.search(key) else item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        ]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
    except ValueError:
        return "<redacted>" if _SECRET_NAME_RE.search(text) else text


def _redact_mcp_text(value: object) -> str:
    text = _redact_mcp_url(value)
    return re.sub(
        r"(?i)((?:--?|/)[^=\s]*(?:api[-_]?key|token|secret|password|authorization|auth|credential)[^=\s]*(?:=|\s+))[^\s]+",
        r"\1<redacted>",
        text,
    )


def _redact_mcp_args(values: object) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    output: list[str] = []
    redact_next = False
    for value in values:
        text = str(value)
        if redact_next:
            output.append("<redacted>")
            redact_next = False
            continue
        match = _SECRET_ARG_RE.match(text)
        if match:
            output.append(f"{match.group(1)}=<redacted>")
            continue
        if text.startswith("--") and _SECRET_NAME_RE.search(text.lstrip("-")):
            output.append(text)
            redact_next = True
            continue
        output.append(_redact_mcp_text(text))
    return output


def _safe_profile_directory(value: object) -> str:
    """Keep Chrome profile identifiers (Default / Profile N), never anything path-like."""
    directory = str(value or "").strip()
    if directory in {"Default", "Guest Profile"}:
        return directory
    if directory.startswith("Profile ") and directory.removeprefix("Profile ").isdigit():
        return directory
    return ""


def _devtools_status() -> dict:
    entry = next(
        (item for item in get_mcp_runtime_diagnostics() if item.get("name") == CHROME_DEVTOOLS_SERVER),
        None,
    )
    if entry is None:
        return {"server": CHROME_DEVTOOLS_SERVER, "running": False, "connected": False, "state": "stopped"}
    return {
        "server": CHROME_DEVTOOLS_SERVER,
        "running": True,
        "connected": bool(entry.get("connected")),
        "state": str(entry.get("state") or ""),
        "startup_phase": str(entry.get("startup_phase") or ""),
        "tool_count": int(entry.get("tool_count") or 0),
        "last_error": str(redact(entry.get("last_error") or "")),
        "unhealthy_reason": str(redact(entry.get("unhealthy_reason") or "")),
        "connected_at": entry.get("connected_at"),
    }


def _browser_status(runtime_settings, runtime_namespace) -> dict:
    available_skills = {item["name"]: item for item in available_skill_payload(runtime_namespace)}
    browser_skill = available_skills.get("browser-use", {})
    profiles = [
        {"name": str(profile.get("name") or ""), "directory": _safe_profile_directory(profile.get("directory"))}
        for profile in list_local_chrome_profiles()
    ]
    devtools = _devtools_status()
    return {
        "skill_enabled": bool(runtime_settings.tools.skills.get("browser-use", False)),
        "skill_available": bool(browser_skill.get("available", False)),
        "skill_unavailable_reason": str(browser_skill.get("unavailable_reason", "") or ""),
        "chrome_found": bool(find_chrome_executable()),
        "chrome_executable": "",
        "available_system_profiles": [profile for profile in profiles if profile["directory"]],
        "system_profile_directory": _safe_profile_directory(runtime_settings.browser.system_profile_directory),
        "default_profile_directory": _safe_profile_directory(last_used_chrome_profile()),
        "devtools": devtools,
        "last_error": devtools.get("last_error", "") or devtools.get("unhealthy_reason", ""),
    }


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
    browser_status = await asyncio.to_thread(_browser_status, runtime_settings, runtime_namespace)
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
            "state": str(mcp_startup.get("state", "") or ""),
            "startup_error": str(mcp_startup.get("startup_error", "") or ""),
        },
        "browser": {
            "chrome_found": browser_status["chrome_found"],
            "devtools_state": browser_status["devtools"]["state"],
            "devtools_connected": browser_status["devtools"]["connected"],
            "last_error": browser_status["last_error"],
        },
        "sandbox": sandbox_status,
    }


@router.get("/diagnostics/mcp", response_model=list[MCPServerDiagnosticsOut])
async def get_mcp_diagnostics():
    runtime_settings = load_agent_settings(settings)
    feature_enabled = bool(runtime_settings.mcp.enabled)
    feature_available = importlib.util.find_spec("mcp") is not None
    feature_unavailable_reason = "" if feature_available else "missing_backend_dependency"
    # Built-in servers belong to their skill, not to the user's MCP list.
    diagnostics = {
        item["name"]: dict(item)
        for item in get_mcp_runtime_diagnostics()
        if not is_builtin_server(item["name"])
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
async def get_browser_status():
    """Chrome discovery, profiles, and the built-in Chrome DevTools connection."""
    runtime_settings = load_agent_settings(settings)
    runtime_namespace = build_runtime_namespace(settings, runtime_settings)
    return await asyncio.to_thread(_browser_status, runtime_settings, runtime_namespace)


@router.post("/diagnostics/browser-use/reset", response_model=BrowserUseDiagnosticsOut)
async def reconnect_browser_devtools():
    """Reconnect the built-in chrome-devtools server. The user's Chrome stays open."""
    await asyncio.to_thread(reconnect_mcp_server, CHROME_DEVTOOLS_SERVER)
    return await get_browser_status()
