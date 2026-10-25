"""MCP binding validation and process-local secrets. No persistence or environment injection."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CredentialBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    server_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    transport: Literal["stdio", "streamable_http"]
    target: Literal["env", "headers"]
    key: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")

    @model_validator(mode="after")
    def transport_matches(self):
        if (self.transport == "stdio") != (self.target == "env"):
            raise ValueError("Credential field does not match transport")
        return self

    @property
    def reference(self) -> str:
        raw = json.dumps([self.server_id, self.transport, self.target, self.key], separators=(",", ":"))
        return "mcp:v1:" + hashlib.sha256(raw.encode()).hexdigest()


def validate_secret(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 20000:
        raise ValueError("A nonempty credential of at most 20000 characters is required")
    if value.strip().lower() in {"[redacted]", "[stored]", "********", "••••••••"} or "\x00" in value:
        raise ValueError("Masked placeholders are not credentials")
    return value


SECRET_NAME = re.compile(r"api[-_]?key|token|secret|password|auth|cookie|credential|bearer|session", re.I)
_LOCK = threading.RLock()
_VALUES: dict[str, str] = {}
# Retain bounded previous values for error/stderr redaction during rotation and teardown.
_REDACTIONS: list[str] = []


def remember_secrets(values) -> None:
    with _LOCK:
        for value in values:
            if value and value not in _REDACTIONS:
                _REDACTIONS.append(value)
        del _REDACTIONS[:-256]


def redact_mcp_secrets(text: str) -> str:
    with _LOCK:
        values = sorted(set([*_REDACTIONS, *_VALUES.values()]), key=len, reverse=True)
    for value in values:
        text = text.replace(value, "[REDACTED]")
    return text


def hydrate(values: dict[str, str]) -> set[str]:
    """Replace the entire runtime snapshot; omitted references are revoked."""
    checked = {ref: validate_secret(value) for ref, value in values.items()}
    with _LOCK:
        changed = {ref for ref in _VALUES.keys() | checked.keys() if _VALUES.get(ref) != checked.get(ref)}
        remember_secrets([*_VALUES.values(), *checked.values()])
        _VALUES.clear()
        _VALUES.update(checked)
        return changed


def revoke(reference: str) -> None:
    with _LOCK:
        value = _VALUES.pop(reference, None)
        if value:
            remember_secrets([value])


def prune_references(servers) -> None:
    allowed = {ref for server in servers for fields in server.credential_refs.values() for ref in fields.values()}
    with _LOCK:
        for reference in list(_VALUES):
            if reference not in allowed:
                revoke(reference)


def resolved_fields(cfg) -> tuple[dict[str, str], dict[str, str]]:
    env, headers = dict(cfg.env), dict(cfg.headers)
    with _LOCK:
        for target, fields in cfg.credential_refs.items():
            for key, ref in fields.items():
                value = _VALUES.get(ref)
                if value is None:
                    raise ValueError("MCP credentials are unavailable; unlock or restore the credential vault")
                (env if target == "env" else headers)[key] = value
    remember_secrets([v for fields in cfg.credential_refs.values() for ref in fields.values()
                      if (v := _VALUES.get(ref)) is not None])
    remember_secrets(v for fields in (env, headers) for k, v in fields.items() if SECRET_NAME.search(k))
    return env, headers


def child_environment(overrides: dict[str, str]) -> dict[str, str]:
    """Keep runtime essentials only. Credentials must be explicitly scoped to a server."""
    import os
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
               "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
               "PROGRAMDATA", "LANG", "LC_ALL", "TERM", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"}
    return {**{key: value for key, value in os.environ.items() if key.upper() in allowed}, **overrides}
