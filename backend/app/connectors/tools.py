"""Expose connector capabilities through ordinary tool discovery and approvals."""

from __future__ import annotations

import hashlib
import json

from .models import ConnectorError, ProviderManifest


def manifest_fingerprint(manifest: ProviderManifest) -> str:
    """Bind queued tools and approvals to the exact adapter version and schemas."""
    return hashlib.sha256(json.dumps(manifest.model_dump(), sort_keys=True, default=sorted).encode()).hexdigest()


def tool_name(provider_id: str, capability_id: str) -> str:
    """Use deterministic names within model providers' 64-character limit."""
    name = f"connector__{provider_id}__{capability_id}"
    return name if len(name) <= 64 else name[:51] + "_" + hashlib.sha256(name.encode()).hexdigest()[:12]


def is_registered_read_tool(name: str) -> bool:
    """Classify only exact tools declared read-only by a registered trusted adapter."""
    from .service import get_connector_service

    return any(tool_name(manifest.id, capability.id) == name and capability.action == "read"
               for manifest in get_connector_service().providers.manifests()
               for capability in manifest.capabilities)


def register_connector_tools(registry, service) -> None:
    """Bind only bundled adapters; credentials are never part of a tool schema."""
    from app.agent.execution_resume import register_executor

    accounts = service.store.list()
    for manifest in service.providers.manifests():
        for capability in manifest.capabilities:
            eligible = [a for a in accounts if a.provider_id == manifest.id and a.enabled
                        and a.state == "ready" and capability.id in a.enabled_capabilities
                        and capability.required_scopes.issubset(a.granted_scopes)]
            if not eligible:
                continue
            name = tool_name(manifest.id, capability.id)
            fingerprint = manifest_fingerprint(manifest)

            def execute(account_id: str, arguments: dict, *, expected_revision=None,
                        expected_fingerprint=fingerprint, _provider=manifest.id,
                        _capability=capability.id, _fingerprint=fingerprint):
                """Use the same validated dispatch for initial calls and approved replay."""
                try:
                    if expected_fingerprint != _fingerprint:
                        raise ConnectorError("account_changed")
                    return json.dumps(service.invoke(_provider, _capability, account_id, arguments,
                        expected_revision=expected_revision, expected_fingerprint=_fingerprint), ensure_ascii=False)
                except ConnectorError as exc:
                    return json.dumps(exc.payload())

            def replay(raw: str, _execute=execute):
                """Replay exact stored arguments, never a model-supplied approval flag."""
                payload = json.loads(raw)
                return _execute(**payload)

            def call(account_id: str, arguments: dict, _execute=execute):
                """Hide internal revision and replay fields from model arguments."""
                return _execute(account_id, arguments)

            register_executor(name, replay)
            account_labels = "; ".join(f"{a.id}: {a.label}" for a in eligible)
            registry.register({
                "name": name, "description": f"{capability.description}\nAccounts: {account_labels}",
                "parameters": {"type": "object", "additionalProperties": False,
                    "properties": {"account_id": {"type": "string", "enum": [a.id for a in eligible]},
                                   "arguments": capability.parameters},
                    "required": ["account_id", "arguments"]},
                "callable": call, "domain": "general", "execution_mode": "sync_thread_affine",
                "affinity_group": f"connector:{manifest.id}", "visible_to_model": False,
                # Conservative durable semantics: do not auto-replay remote writes after a crash.
                "metadata": {"skill": "connectors", "durable_builtin": True,
                    "parallel_safe": False, "mutates_state": capability.action != "read",
                    "search_tags": [manifest.name, manifest.id, capability.id]},
            })
