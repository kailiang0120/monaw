"""Main-process-only MCP vault protocol. Responses never enter renderer state."""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.agent.settings_store import load_agent_settings, save_agent_settings, settings_version
from app.config import settings
from app.security.mcp_credentials import CredentialBinding, hydrate, revoke, validate_secret

router = APIRouter(prefix="/settings/mcp-credentials")


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binding: CredentialBinding
    expected_version: str


class Commit(Selection):
    legacy_digest: str | None = Field(None, pattern=r"^[a-f0-9]{64}$")


class RuntimeValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binding: CredentialBinding
    value: SecretStr


class Hydration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: list[RuntimeValue] = Field(default_factory=list, max_length=256)


class Revocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binding: CredentialBinding


def _server(current, binding):
    server = next((s for s in current.mcp.servers if s.server_id == binding.server_id), None)
    if server is None or server.transport != binding.transport:
        raise HTTPException(409, "MCP server binding changed; reload saved settings")
    return server


def _selected(body):
    current = load_agent_settings(settings)
    if settings_version(current) != body.expected_version:
        raise HTTPException(409, "Settings changed; reload and retry the credential operation")
    return current, _server(current, body.binding)


@router.get("/inventory")
async def inventory():
    current = load_agent_settings(settings)
    return {"settings_version": settings_version(current), "servers": [
        {"server_id": s.server_id, "name": s.name, "transport": s.transport,
         "credential_refs": s.credential_refs,
         "legacy_fields": {target: list(getattr(s, target)) for target in ("env", "headers")}}
        for s in current.mcp.servers
    ]}


@router.post("/prepare")
async def prepare(body: Selection):
    _, server = _selected(body)
    value = getattr(server, body.binding.target).get(body.binding.key)
    if not value:
        raise HTTPException(409, "Selected legacy value is unavailable")
    try:
        validate_secret(value)
    except ValueError:
        raise HTTPException(400, "Selected legacy value is not a usable credential") from None
    return {"value": value, "legacy_digest": hashlib.sha256(value.encode()).hexdigest()}


@router.post("/commit")
async def commit(body: Commit):
    current, server = _selected(body)
    fields = getattr(server, body.binding.target)
    if body.legacy_digest is not None:
        value = fields.get(body.binding.key)
        if value is None or hashlib.sha256(value.encode()).hexdigest() != body.legacy_digest:
            raise HTTPException(409, "Selected legacy value changed; migration was not applied")
    elif body.binding.key in fields:
        raise HTTPException(409, "Migrate the existing plaintext field before replacing it")
    server.credential_refs.setdefault(body.binding.target, {})[body.binding.key] = body.binding.reference
    fields.pop(body.binding.key, None)
    # Validate the canonical binding before the atomic replacement.
    current = type(current).model_validate(current.model_dump())
    save_agent_settings(current)
    # Any pre-migration process may retain the old value. Close it immediately.
    from app.skills.mcp_bridge.connection import get_active_mcp_manager
    manager = get_active_mcp_manager(server.name)
    if manager is not None:
        manager._credential_retired = True
        manager.close()
    from app.skills.mcp_bridge.registry import clear_reflected_tools_for_server
    from app.agent.runtime import reset_runtime_cache
    clear_reflected_tools_for_server(server.name)
    reset_runtime_cache(reset_mcp=False)
    return {"ok": True}


@router.post("/hydrate")
async def hydrate_runtime(body: Hydration):
    current = load_agent_settings(settings)
    values = {}
    for item in body.values:
        server = _server(current, item.binding)
        if server.credential_refs.get(item.binding.target, {}).get(item.binding.key) != item.binding.reference:
            raise HTTPException(409, "Credential reference is not bound to the saved server")
        if item.binding.reference in values:
            raise HTTPException(400, "Duplicate credential binding")
        try:
            values[item.binding.reference] = validate_secret(item.value.get_secret_value())
        except ValueError:
            raise HTTPException(400, "Invalid credential value") from None
    changed = hydrate(values)
    from app.skills.mcp_bridge.connection import get_active_mcp_manager, ensure_mcp_manager
    from app.agent.runtime import reset_runtime_cache
    # Refresh only affected custom connections. Missing values remain fail-closed.
    for server in current.mcp.servers:
        refs = {ref for fields in server.credential_refs.values() for ref in fields.values()}
        manager = get_active_mcp_manager(server.name)
        if refs & changed or (refs and manager is not None and manager.state == "credentials_unavailable"):
            if manager is not None:
                manager._credential_retired = True
                manager.close()
            from app.skills.mcp_bridge.registry import clear_reflected_tools_for_server
            clear_reflected_tools_for_server(server.name)
            if current.mcp.enabled and server.enabled:
                ensure_mcp_manager(server).start()
    if changed:
        reset_runtime_cache(reset_mcp=False)
    return {"ok": True}


@router.post("/revoke")
async def revoke_runtime(body: Revocation):
    current = load_agent_settings(settings)
    server = _server(current, body.binding)
    if server.credential_refs.get(body.binding.target, {}).get(body.binding.key) != body.binding.reference:
        raise HTTPException(409, "Credential reference is not bound to the saved server")
    revoke(body.binding.reference)
    from app.skills.mcp_bridge.connection import get_active_mcp_manager
    from app.agent.runtime import reset_runtime_cache
    manager = get_active_mcp_manager(server.name)
    if manager is not None:
        manager._credential_retired = True
        manager.close()
    from app.skills.mcp_bridge.registry import clear_reflected_tools_for_server
    clear_reflected_tools_for_server(server.name)
    reset_runtime_cache(reset_mcp=False)
    return {"ok": True}
