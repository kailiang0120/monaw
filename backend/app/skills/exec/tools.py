from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any

from app.agent.access_grant_broker import create_grant_ticket
from app.agent.approval_broker import create_ticket
from app.agent.audit import AuditLogger
from app.agent.controller_policy import ActionType, resolve_permission
from app.agent.execution_resume import register_executor
from app.agent.runtime_paths import WORKSPACE_DIR
from app.agent.sandbox.artifacts import write_artifact as _write_artifact
from app.agent.sandbox.environment import SanitizedEnvironment, build_exec_environment
from app.agent.sandbox.manager import SandboxManager, coerce_sandbox_settings
from app.agent.sandbox.models import (
    SandboxDecision,
    SandboxExecutionRequest,
    SandboxRunRequest,
    SandboxSessionStartRequest,
)
from app.agent.sandbox.policy import SandboxPolicy, classify_command
from app.agent.sandbox.sessions import SandboxSessionRegistry
from app.agent.tool_cancellation import (
    current_call_id,
    register_cancellation,
    unregister_cancellation,
)
_audit = AuditLogger()
_MAX_STDOUT = 8 * 1024
_MAX_STDERR = 4 * 1024
_ACTIVE_SETTINGS: Any | None = None
_SESSION_REGISTRY = SandboxSessionRegistry()


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
        "trust_class": decision.trust_class,
        "required_isolation": decision.required_isolation,
        "profile": decision.profile,
        "backend": backend,
        "selected_backend": backend,
        "security_label": decision.security_label,
        "network": decision.network,
        "network_enforcement": decision.network_enforcement,
        "write_strategy": decision.write_strategy,
        "requested_shell": decision.requested_shell,
        "effective_shell": decision.effective_shell,
        "filesystem_policy": decision.filesystem_policy,
        "explicit_approval_required": decision.explicit_approval_required,
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
        requested_access="write",
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
    host_approval_required: bool = False,
    timeout: int = 60,
    host: str = "local",
    elevated: bool = False,
    tool_name: str = "exec",
    bypass_confirmation: bool = False,
    extra_payload: dict[str, Any] | None = None,
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
    if (decision.requires_confirmation or host_approval_required) and not bypass_confirmation:
        if host_approval_required:
            decision = SimpleNamespace(
                reason=str(
                    sandbox.get("reason")
                    or "This command will run on the host without strong isolation."
                ),
                reason_code=str(
                    sandbox.get("reason_code") or "host_execution_approval_required"
                ),
                policy_source="sandbox_policy",
            )
        return _pending_approval(
            decision,
            (
                f"Approve host execution in {workdir}: {command[:120]}"
                if host_approval_required
                else f"Execute command in {workdir}: {command[:120]}"
            ),
            {
                "tool_name": tool_name,
                "command": command,
                "shell": shell,
                "workdir": workdir,
                "env": env or {},
                "timeout": timeout,
                "host": host,
                "elevated": elevated,
                **(extra_payload or {}),
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
    shell: str = "auto",
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

    effective_shell = decision.effective_shell
    sanitized_env, sandbox_meta = _build_sanitized_env(
        shell=effective_shell,
        env=env,
        decision=decision,
        sandbox_meta=sandbox_meta,
    )
    if sanitized_env.blocked:
        return _blocked_env_result(sanitized_env, sandbox_meta)

    pending = _permission_check(
        command=command,
        shell=effective_shell,
        workdir=effective_workdir,
        env=env,
        sandbox=sandbox_meta,
        host_approval_required=decision.explicit_approval_required,
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
        shell=effective_shell,  # type: ignore[arg-type]
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
        copy_policy={"write_strategy": decision.write_strategy},
    )
    call_id = current_call_id()
    cancel_event = threading.Event()
    request.cancel_event = cancel_event
    register_cancellation(call_id, cancel_event.set)
    try:
        result = SandboxManager(getattr(_ACTIVE_SETTINGS, "sandbox", None)).run(request, decision=decision)
    finally:
        unregister_cancellation(call_id)
    _audit.log(
        "exec",
        data={
            "command": command,
            "shell": effective_shell,
            "requested_shell": shell,
            "workdir": effective_workdir,
            "timeout": timeout,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "sandbox_backend": result.sandbox.get("backend"),
            "sandbox_profile": result.sandbox.get("profile"),
            "sandbox_mode": result.sandbox.get("mode"),
            "sandbox_security_label": result.sandbox.get("security_label"),
            "sandbox_trust_class": result.sandbox.get("trust_class"),
            "sandbox_required_isolation": result.sandbox.get("required_isolation"),
            "sandbox_network": result.sandbox.get("network"),
            "sandbox_network_enforcement": result.sandbox.get("network_enforcement"),
            "sandbox_filesystem_policy": result.sandbox.get("filesystem_policy"),
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
    shell: str = "auto",
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

    effective_shell = decision.effective_shell
    sanitized_env, sandbox_meta = _build_sanitized_env(
        shell=effective_shell,
        env=env,
        decision=decision,
        sandbox_meta=sandbox_meta,
    )
    if sanitized_env.blocked:
        return _blocked_env_result(sanitized_env, sandbox_meta)

    pending = _permission_check(
        command=command,
        shell=effective_shell,
        workdir=effective_workdir,
        env=env,
        sandbox=sandbox_meta,
        host_approval_required=decision.explicit_approval_required,
        timeout=0,
        host=host,
        elevated=elevated,
        tool_name="exec_start",
        bypass_confirmation=_bypass_gate,
    )
    if pending:
        return pending

    try:
        _SESSION_REGISTRY.cleanup_stale(
            max_age_seconds=SandboxSessionRegistry.DEFAULT_MAX_AGE_SECONDS
        )
        request = SandboxSessionStartRequest(
            command=command,
            shell=effective_shell,  # type: ignore[arg-type]
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
                "shell": effective_shell,
                "requested_shell": shell,
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
                "shell": effective_shell,
                "requested_shell": shell,
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
                "shell": effective_shell,
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


def exec_write_stdin(command_id: str, text: str, _bypass_gate: bool = False) -> str:
    session = _SESSION_REGISTRY.describe(command_id)
    if session is None:
        return json.dumps(
            {
                "status": "error",
                "error": f"Unknown command_id '{command_id}'",
                "reason_code": "unknown_command",
            },
            ensure_ascii=False,
        )
    if session.get("expired"):
        return json.dumps(
            {
                "status": "error",
                "error": "The exec session exceeded its maximum lifetime.",
                "reason_code": "session_expired",
                "sandbox": session.get("sandbox", {}),
            },
            ensure_ascii=False,
        )

    profile = classify_command(text)
    decision = SandboxPolicy(
        coerce_sandbox_settings(getattr(_ACTIVE_SETTINGS, "sandbox", None))
    ).decide(
        SandboxRunRequest(
            command=text,
            shell=session["shell"],  # type: ignore[arg-type]
            workdir=session["workdir"],
            profile=profile,
            timeout=1,
        )
    )
    sandbox = {
        **session.get("sandbox", {}),
        "stdin_command_profile": profile,
        "stdin_policy_reason": decision.reason,
        "stdin_policy_reason_code": decision.reason_code,
    }
    if not decision.allowed:
        return _blocked_sandbox_result(decision, sandbox)
    session_backend = str(session.get("backend") or session.get("sandbox", {}).get("backend") or "")
    if decision.backend != session_backend:
        return json.dumps(
            {
                "status": "blocked",
                "reason": (
                    f"The stdin policy selected '{decision.backend}', but the existing session "
                    f"runs under '{session_backend}'."
                ),
                "reason_code": "session_backend_mismatch",
                "sandbox": {
                    **sandbox,
                    "session_backend": session_backend,
                    "selected_backend": decision.backend,
                    "reason_code": "session_backend_mismatch",
                },
            },
            ensure_ascii=False,
        )
    pending = _permission_check(
        command=text,
        shell=session["shell"],
        workdir=session["workdir"],
        env={},
        sandbox=sandbox,
        host_approval_required=decision.explicit_approval_required,
        timeout=1,
        host="local",
        tool_name="exec_write_stdin",
        bypass_confirmation=_bypass_gate,
        extra_payload={"command_id": command_id, "text": text},
    )
    if pending:
        return pending
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
        shell=str(args.get("shell", "auto")),
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
        shell=str(args.get("shell", "auto")),
        workdir=str(args.get("workdir", "")),
        env=args.get("env") if isinstance(args.get("env"), dict) else {},
        host=str(args.get("host", "local")),
        elevated=bool(args.get("elevated", False)),
        _bypass_gate=True,
    )


register_executor("exec_start", _resume_exec_start)


def _resume_exec_write_stdin(input_str: str) -> str:
    args = _parse_json_object(input_str)
    return exec_write_stdin(
        command_id=str(args.get("command_id", "")),
        text=str(args.get("text", "")),
        _bypass_gate=True,
    )


register_executor("exec_write_stdin", _resume_exec_write_stdin)


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
                        "enum": ["auto", "powershell", "pwsh", "cmd", "bash"],
                        "default": "auto",
                    },
                    "workdir": {"type": "string", "default": ""},
                    "env": {"type": "object", "default": {}},
                    "timeout": {"type": "integer", "default": 60},
                    "host": {"type": "string", "enum": ["local"], "default": "local"},
                    "max_stdout": {"type": "integer", "default": _MAX_STDOUT},
                    "max_stderr": {"type": "integer", "default": _MAX_STDERR},
                    "tail_lines": {"type": "integer", "default": 0},
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
                    "shell": {"type": "string", "enum": ["auto", "powershell", "pwsh", "cmd", "bash"], "default": "auto"},
                    "workdir": {"type": "string", "default": ""},
                    "env": {"type": "object", "default": {}},
                    "host": {"type": "string", "enum": ["local"], "default": "local"},
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
