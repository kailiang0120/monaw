"""A support report the user reads before sharing: bounded, redacted, and free of setting values."""

from __future__ import annotations

import json
import platform
import sys
from typing import Any

from app.agent.observability.redaction import redact

REPORT_FORMAT = "monaw-support-report/1"
MAX_REPORT_BYTES = 512 * 1024
MAX_ERRORS = 50
MAX_LOG_LINES = 300


def settings_shape(runtime_settings: Any) -> dict[str, Any]:
    """What is configured, never the values: no keys, paths, names, prompts or server commands."""
    llm = runtime_settings.llm
    sandbox = runtime_settings.sandbox
    mcp = runtime_settings.mcp
    skills = getattr(getattr(runtime_settings, "tools", None), "skills", {}) or {}
    return {
        "model_provider": llm.provider,
        "model": llm.model_name,
        "permission_mode": runtime_settings.permissions.mode,
        "sandbox": {
            "enabled": bool(sandbox.enabled),
            "mode": sandbox.mode,
            "docker_enabled": bool(sandbox.docker.enabled),
            "network": sandbox.network.default,
        },
        "memory_enabled": bool(runtime_settings.memory.enabled),
        "mcp": {
            "enabled": bool(mcp.enabled),
            "servers": len(mcp.servers),
            "enabled_servers": sum(1 for server in mcp.servers if server.enabled),
        },
        "skills_enabled": sorted(name for name, enabled in skills.items() if enabled),
    }


def _size(report: dict[str, Any]) -> int:
    return len(json.dumps(report, ensure_ascii=False).encode("utf-8"))


def build_support_report(
    *,
    created_at: str,
    app_version: str,
    summary: dict[str, Any],
    configuration: dict[str, Any],
    errors: list[dict[str, Any]],
    log_lines: list[str] | None,
) -> dict[str, Any]:
    """Assemble the report. ``log_lines`` is None when support mode is off, so the log is left out."""
    report: dict[str, Any] = {
        "format": REPORT_FORMAT,
        "created_at": created_at,
        "application": {
            "version": app_version,
            "packaged": bool(getattr(sys, "frozen", False)),
            "python": platform.python_version(),
            "platform": platform.platform(terse=True),
        },
        "diagnostics": summary,
        "configuration": configuration,
        "recent_errors": list(errors[:MAX_ERRORS]),
        "backend_log": {
            "included": log_lines is not None,
            "lines": list((log_lines or [])[-MAX_LOG_LINES:]),
            "note": "" if log_lines is not None else "Turn on support mode to include the backend log.",
        },
        "truncated": False,
    }
    report = redact(report)

    # Stay under the size limit by dropping the oldest log lines first, then the oldest errors.
    while _size(report) > MAX_REPORT_BYTES and report["backend_log"]["lines"]:
        lines = report["backend_log"]["lines"]
        report["backend_log"]["lines"] = lines[len(lines) // 2 + 1:]
        report["truncated"] = True
    while _size(report) > MAX_REPORT_BYTES and report["recent_errors"]:
        report["recent_errors"] = report["recent_errors"][: len(report["recent_errors"]) // 2]
        report["truncated"] = True
    return report
