from __future__ import annotations

import json
from typing import Any

from app.agent.access_grant_broker import create_grant_ticket
from app.agent.approval_broker import create_ticket
from app.agent.audit import AuditLogger
from app.agent.controller_policy import ActionType, resolve_permission
from app.agent.execution_resume import register_executor
from app.agent.runtime_paths import RUNTIME_DIR, WORKSPACE_DIR
from app.agent.sandbox.environment import SanitizedEnvironment, build_exec_environment
from app.agent.sandbox.manager import SandboxManager, coerce_sandbox_settings
from app.agent.sandbox.models import (
    SandboxDecision,
    SandboxExecutionRequest,
    SandboxRunRequest,
    SandboxSessionStartRequest,
)
from app.agent.sandbox.policy import SandboxPolicy
from app.agent.sandbox.sessions import SandboxSessionRegistry

_audit = AuditLogger()
_MAX_STDOUT = 8 * 1024
_MAX_STDERR = 4 * 1024
_EXEC_ARTIFACT_DIR = RUNTIME_DIR / "exec"
_EXEC_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
_ACTIVE_SETTINGS: Any | None = None
_SESSION_REGISTRY = SandboxSessionRegistry()


def _write_artifact(command_id: str, stream_name: str, content: str) -> str:
    path = _EXEC_ARTIFACT_DIR / f"{command_id}.{stream_name}.txt"
    path.write_text(content, encoding="utf-8", newline="")
    return str(path)


def _safe_env_keys(env: dict[str, str] | None) -> list[str]:
    return sorted(str(key) for key in (env or {}).keys())


def _redacted_env(env: dict[str, str] | None) -> dict[str, str]:
    return {str(key): "<redacted>" for key in (env or {}).keys()}


def _execution_backend(decision: SandboxDecision) -> str:
    return "local_direct" if decision.backend == "none" else decision.backend


def build_sandbox_decision(
    *,
    command: str,
    shell: str,
    workdir: str,
    env: dict[str, str] | None,
    timeout: int,
    elevated: bool,
) -> SandboxDecision:
    sandbox_settings = coerce_sandbox_settings(getattr(_ACTIVE_SETTINGS, "sandbox", None))
    return SandboxPolicy(sandbox_settings).decide(
        SandboxRunRequest(
            command=command,
            shell=shell,  # type: ignore[arg-type]
            workdir=workdir,
            env=env or {},
            timeout=timeout,
            elevated=elevated,
        )
    )


def _sandbox_metadata(decision: SandboxDecision, workdir: str) -> dict[str, Any]:
    backend = _execution_backend(decision) if decision.allowed else decision.backend
    return {
        "enabled": decision.required or decision.mode not in {"off", "disabled"},
        "mode": decision.mode,
        "profile": decision.profile,
        "backend": backend,
        "selected_backend": backend,
        "security_label": decision.security_label,
        "network": decision.network,
        "network_enforcement": decision.network_enforcement,
        "write_strategy": decision.write_strategy,
        "reason": decision.reason,
        "reason_code": decision.reason_code,
        "workdir": workdir,
    }


def _build_sanitized_env(
    *,
    shell: str,
    env: dict[str, str] | None,
    decision: SandboxDecision,
    sandbox_meta: dict[str, Any],
) -> tuple[SanitizedEnvironment, dict[str, Any]]:
    sanitized_env = build_exec_environment(
        requested_env=env or {},
        shell=shell,
        profile=decision.profile,
        backend=_execution_backend(decision),
    )
    return sanitized_env, {**sandbox_meta, **sanitized_env.metadata()}


def _blocked_sandbox_result(decision: SandboxDecision, sandbox: dict[str, Any]) -> str:
    return json.dumps(
        {
            "status": "blocked",
            "reason": decision.reason,
            "reason_code": decision.reason_code,
            "sandbox": sandbox,
        },
        ensure_ascii=False,
    )


def _blocked_env_result(sanitized_env: SanitizedEnvironment, sandbox: dict[str, Any]) -> str:
    return json.dumps(
        {
            "status": "blocked",
            "reason": sanitized_env.reason,
            "reason_code": sanitized_env.reason_code,
            "blocked_env_keys": sanitized_env.blocked_keys,
            "sandbox": sandbox,
        },
        ensure_ascii=False,
    )


def _sandbox_resources() -> dict[str, Any]:
    sandbox_settings = getattr(_ACTIVE_SETTINGS, "sandbox", None)
    resources = getattr(sandbox_settings, "resources", {}) if sandbox_settings is not None else {}
    if hasattr(resources, "model_dump"):
        return dict(resources.model_dump())
    if isinstance(resources, dict):
        return dict(resources)
    return {}


def _pending_approval(decision, action_desc: str, payload: dict, sandbox: dict[str, Any] | None = None) -> str:
    payload_args = dict(payload)
    env_keys = _safe_env_keys(payload_args.get("env"))
    payload_args["env"] = _redacted_env(payload_args.get("env"))
    payload_args["env_keys"] = env_keys
    if sandbox is not None:
        payload_args["sandbox"] = sandbox
    ticket = create_ticket(
        action_type="exec",
        tool_name=str(payload.get("tool_name") or "exec"),
        target_path=payload.get("workdir", ""),
        risk_level="high" if payload.get("elevated") else "medium",
        reason=decision.reason,
        action_description=action_desc,
        payload={
            "input_str": json.dumps(payload_args, ensure_ascii=False, sort_keys=True),
            "args": payload_args,
        },
    )
    return json.dumps(
        {
            "status": "pending_approval",
            "ticket_id": ticket.id,
            "action": action_desc,
            "reason": decision.reason,
            "reason_code": decision.reason_code,
            "policy_source": decision.policy_source,
            "command": payload.get("command", ""),
            "shell": payload.get("shell", ""),
            "workdir": payload.get("workdir", ""),
            "env_keys": env_keys,
            "sandbox": sandbox or {},
        }
    )


def _pending_access_grant(workdir: str) -> str:
    ticket = create_grant_ticket(
        target_type="path",
        target_identifier=workdir,
        display_name=workdir,
        action_context=f"Exec in workdir: {workdir}",
    )
    return json.dumps(
        {
            "status": "pending_access_grant",
            "ticket_id": ticket.id,
            "target_type": ticket.target_type,
            "target_identifier": ticket.target_identifier,
            "display_name": ticket.display_name,
            "action_context": ticket.action_context,
        }
    )


def _effective_workdir(workdir: str) -> str:
    return workdir or str(WORKSPACE_DIR)


def _permission_check(
    *,
    command: str,
    shell: str,
    workdir: str,
    env: dict[str, str] | None,
    sandbox: dict[str, Any],
    timeout: int = 60,
    host: str = "local",
    elevated: bool = False,
    tool_name: str = "exec",
    bypass_confirmation: bool = False,
) -> str | None:
    if host != "local":
        return json.dumps({"status": "error", "error": f"Unsupported host '{host}'"})

    decision = resolve_permission(ActionType.EXEC, target_path=workdir)
    if decision.blocked:
        return json.dumps(
            {
                "status": "blocked",
                "reason": decision.reason,
                "reason_code": decision.reason_code,
                "policy_source": decision.policy_source,
                "sandbox": sandbox,
            }
        )
    if decision.requires_access_grant:
        return _pending_access_grant(workdir)
    if decision.requires_confirmation and not bypass_confirmation:
        return _pending_approval(
            decision,
            f"Execute command in {workdir}: {command[:120]}",
            {
                "tool_name": tool_name,
                "command": command,
                "shell": shell,
                "workdir": workdir,
                "env": env or {},
                "timeout": timeout,
                "host": host,
                "elevated": elevated,
            },
            sandbox,
        )
    return None


def _session_unsupported_result(decision: SandboxDecision, sandbox: dict[str, Any]) -> str:
    return json.dumps(
        {
            "status": "blocked",
            "reason": "Sandboxed long-running sessions are not supported for the selected backend yet.",
            "reason_code": "sandbox_sessions_unsupported",
            "sandbox": {
                **sandbox,
                "reason": "Sandboxed long-running sessions are not supported for the selected backend yet.",
                "reason_code": "sandbox_sessions_unsupported",
            },
        },
        ensure_ascii=False,
    )


def exec_tool(
    command: str,
    shell: str = "powershell",
    workdir: str = "",
    env: dict[str, str] | None = None,
    timeout: int = 60,
    host: str = "local",
    elevated: bool = False,
    max_stdout: int = _MAX_STDOUT,
    max_stderr: int = _MAX_STDERR,
    tail_lines: int = 0,
    save_output_to: str = "",
    return_mode: str = "full",
    _bypass_gate: bool = False,
) -> str:
    effective_workdir = _effective_workdir(workdir)
    decision = build_sandbox_decision(
        command=command,
        shell=shell,
        workdir=effective_workdir,
        env=env,
        timeout=timeout,
        elevated=elevated,
    )
    sandbox_meta = _sandbox_metadata(decision, effective_workdir)
    if not decision.allowed:
        return _blocked_sandbox_result(decision, sandbox_meta)

    sanitized_env, sandbox_meta = _build_sanitized_env(
        shell=shell,
        env=env,
        decision=decision,
        sandbox_meta=sandbox_meta,
    )
    if sanitized_env.blocked:
        return _blocked_env_result(sanitized_env, sandbox_meta)

    pending = _permission_check(
        command=command,
        shell=shell,
        workdir=effective_workdir,
        env=env,
        sandbox=sandbox_meta,
        timeout=timeout,
        host=host,
        elevated=elevated,
        tool_name="exec",
        bypass_confirmation=_bypass_gate,
    )
    if pending:
        return pending

    request = SandboxExecutionRequest(
        command=command,
        shell=shell,  # type: ignore[arg-type]
        workdir=effective_workdir,
        env=sanitized_env.env,
        timeout=timeout,
        max_stdout=max_stdout,
        max_stderr=max_stderr,
        tail_lines=tail_lines,
        save_output_to=save_output_to,
        return_mode=return_mode,  # type: ignore[arg-type]
        profile=decision.profile,
        backend=_execution_backend(decision),
        network=decision.network,
        tool_name="exec",
        env_metadata=sandbox_meta,
        resources=_sandbox_resources(),
    )
    result = SandboxManager(getattr(_ACTIVE_SETTINGS, "sandbox", None)).run(request)
    _audit.log(
        "exec",
        data={
            "command": command,
            "shell": shell,
            "workdir": effective_workdir,
            "timeout": timeout,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "sandbox_backend": result.sandbox.get("backend"),
            "sandbox_profile": result.sandbox.get("profile"),
            "sandbox_mode": result.sandbox.get("mode"),
            "sandbox_security_label": result.sandbox.get("security_label"),
            "sandbox_network": result.sandbox.get("network"),
            "env_inheritance": result.sandbox.get("env_inheritance"),
            "env_keys": result.sandbox.get("env_keys", []),
            "explicit_env_keys": sanitized_env.explicit_keys,
            "blocked_env_keys": sanitized_env.blocked_keys,
            "reason_code": result.reason_code,
        },
        success=result.status == "ok",
        error=(result.stderr or result.error or result.reason)[:400],
    )
    return json.dumps(result.model_dump(), ensure_ascii=False)


def exec_start(
    command: str,
    shell: str = "powershell",
    workdir: str = "",
    env: dict[str, str] | None = None,
    host: str = "local",
    elevated: bool = False,
    _bypass_gate: bool = False,
) -> str:
    effective_workdir = _effective_workdir(workdir)
    decision = build_sandbox_decision(
        command=command,
        shell=shell,
        workdir=effective_workdir,
        env=env,
        timeout=0,
        elevated=elevated,
    )
    sandbox_meta = _sandbox_metadata(decision, effective_workdir)
    if not decision.allowed and decision.reason_code in {"command_blocked", "elevated_blocked"}:
        return _blocked_sandbox_result(decision, sandbox_meta)
    if decision.mode in {"docker", "enforce"} or decision.backend == "docker":
        return _session_unsupported_result(decision, sandbox_meta)
    if not decision.allowed:
        return _blocked_sandbox_result(decision, sandbox_meta)

    sanitized_env, sandbox_meta = _build_sanitized_env(
        shell=shell,
        env=env,
        decision=decision,
        sandbox_meta=sandbox_meta,
    )
    if sanitized_env.blocked:
        return _blocked_env_result(sanitized_env, sandbox_meta)

    pending = _permission_check(
        command=command,
        shell=shell,
        workdir=effective_workdir,
        env=env,
        sandbox=sandbox_meta,
        timeout=0,
        host=host,
        elevated=elevated,
        tool_name="exec_start",
        bypass_confirmation=_bypass_gate,
    )
    if pending:
        return pending

    try:
        request = SandboxSessionStartRequest(
            command=command,
            shell=shell,  # type: ignore[arg-type]
            workdir=effective_workdir,
            env=sanitized_env.env,
            profile=decision.profile,
            backend=_execution_backend(decision),
            network=decision.network,
            tool_name="exec_start",
            env_metadata=sandbox_meta,
            resources=_sandbox_resources(),
        )
        if request.backend == "local_restricted":
            handle = _SESSION_REGISTRY.start_local_restricted(request)
        else:
            handle = _SESSION_REGISTRY.start_local_direct(request)
        _audit.log(
            "exec_start",
            data={
                "command": command,
                "shell": shell,
                "workdir": effective_workdir,
                "pid": handle.pid,
                "sandbox_backend": handle.sandbox.get("backend"),
                "sandbox_profile": handle.sandbox.get("profile"),
                "env_inheritance": handle.sandbox.get("env_inheritance"),
                "env_keys": handle.sandbox.get("env_keys", []),
                "explicit_env_keys": sanitized_env.explicit_keys,
                "blocked_env_keys": sanitized_env.blocked_keys,
            },
            success=True,
        )
        return json.dumps(
            {
                "status": "running",
                "command_id": handle.session_id,
                "pid": handle.pid,
                "shell": shell,
                "workdir": effective_workdir,
                "stdout_path": _write_artifact(handle.session_id, "stdout", ""),
                "stderr_path": _write_artifact(handle.session_id, "stderr", ""),
                "env_keys": sanitized_env.explicit_keys,
                "sandbox": handle.sandbox,
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        _audit.log(
            "exec_start",
            data={
                "command": command,
                "shell": shell,
                "workdir": effective_workdir,
                "sandbox_backend": sandbox_meta["backend"],
                "sandbox_profile": sandbox_meta["profile"],
                "env_inheritance": sandbox_meta["env_inheritance"],
                "env_keys": sandbox_meta["env_keys"],
                "explicit_env_keys": sanitized_env.explicit_keys,
                "blocked_env_keys": sanitized_env.blocked_keys,
            },
            success=False,
            error=str(exc),
        )
        return json.dumps({"status": "error", "error": str(exc), "sandbox": sandbox_meta})


def exec_poll(command_id: str, max_output: int = _MAX_STDOUT, tail_lines: int = 200, return_mode: str = "tail") -> str:
    payload = _SESSION_REGISTRY.poll(
        command_id,
        max_output=max_output,
        tail_lines=tail_lines,
        return_mode=return_mode,
    )
    return json.dumps(payload.model_dump(), ensure_ascii=False)


def exec_write_stdin(command_id: str, text: str) -> str:
    return json.dumps(_SESSION_REGISTRY.write_stdin(command_id, text), ensure_ascii=False)


def exec_stop(command_id: str, signal: str = "terminate") -> str:
    payload = _SESSION_REGISTRY.stop(command_id, signal)
    return json.dumps(payload.model_dump(), ensure_ascii=False)


def _parse_json_object(input_str: str) -> dict[str, Any]:
    try:
        value = json.loads(input_str) if input_str else {}
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _resume_exec(input_str: str) -> str:
    args = _parse_json_object(input_str)
    return exec_tool(
        command=str(args.get("command", "")),
        shell=str(args.get("shell", "powershell")),
        workdir=str(args.get("workdir", "")),
        env=args.get("env") if isinstance(args.get("env"), dict) else {},
        timeout=int(args.get("timeout", 60) or 60),
        host=str(args.get("host", "local")),
        elevated=bool(args.get("elevated", False)),
        _bypass_gate=True,
    )


register_executor("exec", _resume_exec)


def _resume_exec_start(input_str: str) -> str:
    args = _parse_json_object(input_str)
    return exec_start(
        command=str(args.get("command", "")),
        shell=str(args.get("shell", "powershell")),
        workdir=str(args.get("workdir", "")),
        env=args.get("env") if isinstance(args.get("env"), dict) else {},
        host=str(args.get("host", "local")),
        elevated=bool(args.get("elevated", False)),
        _bypass_gate=True,
    )


register_executor("exec_start", _resume_exec_start)


def register_tools(registry, _settings=None) -> None:
    global _ACTIVE_SETTINGS
    _ACTIVE_SETTINGS = _settings
    registry.extend(
        [
        {
            "name": "exec",
            "description": "Execute a local shell command with policy checks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command to execute."},
                    "shell": {
                        "type": "string",
                        "enum": ["powershell", "pwsh", "cmd", "bash"],
                        "default": "powershell",
                    },
                    "workdir": {"type": "string", "default": ""},
                    "env": {"type": "object", "default": {}},
                    "timeout": {"type": "integer", "default": 60},
                    "host": {"type": "string", "enum": ["local"], "default": "local"},
                    "elevated": {"type": "boolean", "default": False},
                    "max_stdout": {"type": "integer", "default": _MAX_STDOUT},
                    "max_stderr": {"type": "integer", "default": _MAX_STDERR},
                    "tail_lines": {"type": "integer", "default": 0},
                    "save_output_to": {"type": "string", "default": ""},
                    "return_mode": {
                        "type": "string",
                        "enum": ["full", "head_tail", "tail", "summary"],
                        "default": "full",
                    },
                },
                "required": ["command"],
            },
            "callable": exec_tool,
            "domain": "general",
            "execution_mode": "sync_stateless",
            "affinity_group": None,
            "metadata": {"parallel_safe": False, "resource_locks": ["exec"], "mutates_state": True, "risk_level": "medium"},
        },
        {
            "name": "exec_start",
            "description": "Start a long-running local command and return a command_id for polling.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "shell": {"type": "string", "enum": ["powershell", "pwsh", "cmd", "bash"], "default": "powershell"},
                    "workdir": {"type": "string", "default": ""},
                    "env": {"type": "object", "default": {}},
                    "host": {"type": "string", "enum": ["local"], "default": "local"},
                    "elevated": {"type": "boolean", "default": False},
                },
                "required": ["command"],
            },
            "callable": exec_start,
            "domain": "general",
            "execution_mode": "sync_stateless",
            "affinity_group": None,
            "metadata": {"parallel_safe": False, "resource_locks": ["exec"], "mutates_state": True, "risk_level": "medium"},
        },
        {
            "name": "exec_poll",
            "description": "Poll a long-running exec session and return recent output plus artifact paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command_id": {"type": "string"},
                    "max_output": {"type": "integer", "default": _MAX_STDOUT},
                    "tail_lines": {"type": "integer", "default": 200},
                    "return_mode": {"type": "string", "enum": ["full", "head_tail", "tail", "summary"], "default": "tail"},
                },
                "required": ["command_id"],
            },
            "callable": exec_poll,
            "domain": "general",
            "execution_mode": "sync_stateless",
            "affinity_group": None,
            "metadata": {"parallel_safe": True, "resource_locks": [], "mutates_state": False, "risk_level": "low"},
        },
        {
            "name": "exec_write_stdin",
            "description": "Write text to a running exec session's stdin.",
            "parameters": {
                "type": "object",
                "properties": {"command_id": {"type": "string"}, "text": {"type": "string"}},
                "required": ["command_id", "text"],
            },
            "callable": exec_write_stdin,
            "domain": "general",
            "execution_mode": "sync_stateless",
            "affinity_group": None,
            "metadata": {"parallel_safe": False, "resource_locks": ["exec"], "mutates_state": True, "risk_level": "medium"},
        },
        {
            "name": "exec_stop",
            "description": "Stop a running exec session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command_id": {"type": "string"},
                    "signal": {"type": "string", "enum": ["terminate", "kill"], "default": "terminate"},
                },
                "required": ["command_id"],
            },
            "callable": exec_stop,
            "domain": "general",
            "execution_mode": "sync_stateless",
            "affinity_group": None,
            "metadata": {"parallel_safe": False, "resource_locks": ["exec"], "mutates_state": True, "risk_level": "medium"},
        },
        ]
    )
