"""Conservative secret handling for previews, diagnostics and file boundaries.

These labels protect known locations; they do not certify arbitrary content safe.
"""
from __future__ import annotations

import os
import json
import re
from pathlib import Path
from typing import Any

_SECRET_KEY = re.compile(r"password|passwd|secret|token|api[_-]?key|authorization|cookie|credential|private[_-]?key", re.I)
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z ]*PRIVATE KEY|OPENSSH PRIVATE KEY)-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S)
_ASSIGNMENT = re.compile(r"(?i)(\b(?:password|passwd|secret|token|api[_-]?key|authorization|cookie)\b[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_URL_SECRET = re.compile(r"(?i)([?&](?:token|key|api_key|password|secret|access_token)=)[^&#\s]+")


def sensitive_path_labels(path: str | Path, sensitive_paths=()) -> list[str]:
    candidate = Path(path).expanduser().resolve(strict=False)
    parts = {part.casefold() for part in candidate.parts}
    name = candidate.name.casefold()
    labels: list[str] = []
    if parts & {".ssh", ".gnupg", ".aws", ".azure", ".kube"}:
        labels.append("credential_directory")
    if name in {".env", ".netrc", "credentials", "credentials.json", "token.json", "tokens.json", "login data", "cookies", "web data"} or name.startswith(".env."):
        labels.append("credential_file")
    if name in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"} or candidate.suffix.casefold() in {".key", ".p12", ".pfx", ".pem"}:
        labels.append("private_key_or_certificate")
    for raw in sensitive_paths:
        root = Path(os.path.expandvars(str(raw))).expanduser().resolve(strict=False)
        if candidate == root or candidate.is_relative_to(root):
            labels.append("user_sensitive")
            break
    return labels


def redact_sensitive(value: Any) -> Any:
    """Return a sanitized copy. Pattern matching supplements known-secret redaction."""
    if isinstance(value, dict):
        return {str(key): "[REDACTED]" if str(key) != "authorization_binding" and _SECRET_KEY.search(str(key)) else redact_sensitive(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item) for item in value]
    if not isinstance(value, str):
        return value
    from app.security.mcp_credentials import redact_mcp_secrets
    result = redact_mcp_secrets(value)
    # Environment credentials are never copied into diagnostic output.
    for key, secret in os.environ.items():
        if _SECRET_KEY.search(key) and len(secret) >= 4:
            result = result.replace(secret, "[REDACTED]")
    result = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", result)
    result = _BEARER.sub("Bearer [REDACTED]", result)
    result = _ASSIGNMENT.sub(lambda m: m.group(1) + "[REDACTED]", result)
    result = _URL_SECRET.sub(lambda m: m.group(1) + "[REDACTED]", result)
    return re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", result)


def public_tool_arguments(tool_name: str, arguments: Any) -> dict[str, Any]:
    """Safe event/trace projection. Never use this copy to execute or approve."""
    if not isinstance(arguments, dict):
        return {"summary": "Tool arguments omitted"}
    if tool_name.startswith("mcp__chrome-devtools__"):
        projected = {key: arguments[key] for key in ("pageId", "uid", "from_uid", "to_uid", "type", "action")
                     if key in arguments and isinstance(arguments[key], (str, int, bool))}
        if arguments.get("url"):
            try:
                from .destination_policy import canonical_origin
                projected["origin"] = canonical_origin(arguments["url"])
            except ValueError:
                projected["origin"] = "Unavailable"
        if isinstance(arguments.get("filePaths"), list):
            projected["file_count"] = len(arguments["filePaths"])
        if set(arguments) - set(projected):
            projected["details"] = "Sensitive input omitted"
        return redact_sensitive(projected)
    if tool_name.startswith("mcp__"):
        return {"summary": "Private MCP arguments omitted"}
    # Existing structured local tools may retain safe paths/flags; free text,
    # shell commands, scripts and file bytes never become ordinary previews.
    hidden = {"content", "text", "value", "function", "script", "code", "command", "input_str", "elements", "stdin"}
    return redact_sensitive({str(key): "[OMITTED]" if str(key).lower() in hidden else item
                             for key, item in arguments.items()})


def public_tool_input(tool_name: str, value: Any) -> Any:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return "Tool input omitted"
        return json.dumps(public_tool_arguments(tool_name, parsed), ensure_ascii=False)
    return public_tool_arguments(tool_name, value)


def install_logging_redaction() -> None:
    """Sanitize standard logging before any handler formats message/traceback."""
    import logging
    import traceback
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_monaw_secret_redaction", False):
        return
    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) == 5:
            # AccessFormatter consumes the five structured arguments separately
            # from getMessage; preserve the tuple and numeric status type.
            record.args = tuple(redact_sensitive(item) for item in record.args)
        else:
            record.msg = redact_sensitive(record.getMessage())
            record.args = ()
        if record.exc_info:
            record._monaw_error_type = str(getattr(record.exc_info[0], "__name__", ""))
            record.exc_text = redact_sensitive("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        return record
    factory._monaw_secret_redaction = True
    logging.setLogRecordFactory(factory)
