"""One disclosure for a shell execution's effective policy, never its settings alone."""
from __future__ import annotations

from typing import Any


def execution_environment(metadata: dict[str, Any], *, blocked: bool = False) -> dict[str, Any]:
    backend = str(metadata.get("backend") or "none")
    docker = backend == "docker"
    strategy = str(metadata.get("write_strategy") or "discard")
    if docker:
        summary = "Runs in Docker"
        filesystem = {
            "direct_rw": "Container filesystem; workspace mounted read/write",
            "copy_out": "Container filesystem; a temporary copy of your workspace is mounted",
            "discard": "Container filesystem; host workspace is not exposed",
        }.get(strategy, "Filesystem behavior unavailable")
        writes = {
            "discard": "Workspace changes are discarded",
            "copy_out": "Changed and new workspace files are copied back; deletions are not propagated",
            "direct_rw": "Changes and deletions write directly to your workspace",
        }.get(strategy, "Write behavior unavailable")
        network = "Network denied by Docker" if metadata.get("network") == "deny" and metadata.get("network_enforcement") == "enforced" else "Network access allowed; no network isolation"
    elif backend in {"local_direct", "local_restricted"}:
        summary = "Runs on your PC — no filesystem or network isolation"
        filesystem = "Host filesystem accessible with your account's permissions"
        network = "Host network accessible; configured network deny is not enforced"
        writes = "Writes and deletions affect host files directly; discard and copy-out do not apply"
        if backend == "local_restricted":
            summary += " (advisory limits only)"
    else:
        summary = "No execution backend selected"
        filesystem = network = writes = "No execution"
    return {
        "backend": backend,
        "summary": "Blocked before execution" if blocked else summary,
        "workspace": str(metadata.get("host_workdir") or metadata.get("workdir") or ""),
        "execution_workspace": "/workspace" if docker else str(metadata.get("workdir") or ""),
        "shell": str(metadata.get("effective_shell") or ""),
        "image": str((metadata.get("runtime_policy") or {}).get("image") or ""),
        "filesystem": filesystem,
        "network": network,
        "writes": writes,
        "approval_required": bool(metadata.get("explicit_approval_required")),
        "reason": str(metadata.get("reason") or ""),
        "scope": "Shell command only. Desktop automation, personal Chrome, and external MCP tools use their own permissions and environments.",
    }


def consent_environment(metadata: dict[str, Any]) -> dict[str, Any]:
    """Fields that must still match when replaying an approved shell operation."""
    return {key: metadata.get(key) for key in (
        "backend", "workdir", "effective_shell", "mode", "profile", "required_isolation",
        "network", "network_enforcement", "filesystem_policy", "write_strategy",
        "explicit_approval_required",
        "runtime_policy",
    )}


def runtime_policy(settings: Any, backend: str) -> dict[str, Any]:
    if backend != "docker":
        return {}
    return {
        "image": settings.docker.image,
        "read_only_root": settings.docker.read_only_root,
        "allowed_bind_roots": list(settings.allowed_bind_roots),
        "blocked_bind_roots": list(settings.blocked_bind_roots),
        "resources": settings.resources.model_dump(),
    }
