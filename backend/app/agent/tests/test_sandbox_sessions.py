import json
import os
import shlex
import sys
import time

from app.agent.sandbox.environment import build_exec_environment
from app.agent.sandbox.models import SandboxDecision, SandboxSessionStartRequest
from app.agent.settings_store import AgentSettings
from app.agent.sandbox.sessions import SandboxSessionRegistry
from app.skills.exec import tools as exec_tools

from .test_sandbox_environment import _allow_exec


def _python_command(code: str) -> tuple[str, str]:
    if os.name == "nt":
        return "powershell", f'& "{sys.executable}" -c "{code}"'
    return "bash", f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def _session_request(command: str, shell: str) -> SandboxSessionStartRequest:
    env = build_exec_environment(
        requested_env={},
        shell=shell,
        profile="standard",
        backend="local_direct",
    )
    return SandboxSessionStartRequest(
        command=command,
        shell=shell,  # type: ignore[arg-type]
        env=env.env,
        profile="standard",
        env_metadata={"mode": "auto", **env.metadata()},
    )


def test_exec_start_uses_session_registry(monkeypatch, tmp_path):
    _allow_exec(monkeypatch)
    shell, command = _python_command("print('session-ok')")

    started = json.loads(exec_tools.exec_start(command, shell=shell, workdir=str(tmp_path), _bypass_gate=True))
    command_id = started["command_id"]

    for _ in range(30):
        polled = json.loads(exec_tools.exec_poll(command_id, return_mode="full"))
        if polled.get("exit_code") is not None:
            break
        time.sleep(0.05)

    assert polled["status"] == "ok"
    assert polled["stdout"].strip() == "session-ok"
    assert polled["sandbox"]["backend"] in {"local_direct", "local_restricted"}


def test_exec_write_stdin_works_for_local_session(monkeypatch, tmp_path):
    _allow_exec(monkeypatch)
    shell, command = _python_command("import sys; print(sys.stdin.readline().strip())")

    started = json.loads(exec_tools.exec_start(command, shell=shell, workdir=str(tmp_path), _bypass_gate=True))
    command_id = started["command_id"]
    written = json.loads(exec_tools.exec_write_stdin(command_id, "hello\n", _bypass_gate=True))

    for _ in range(30):
        polled = json.loads(exec_tools.exec_poll(command_id, return_mode="full"))
        if polled.get("exit_code") is not None:
            break
        time.sleep(0.05)

    assert written["status"] == "ok"
    assert polled["stdout"].strip() == "hello"


def test_exec_stop_cleans_backend_session(monkeypatch, tmp_path):
    _allow_exec(monkeypatch)
    shell, command = _python_command("import time; time.sleep(5)")

    started = json.loads(exec_tools.exec_start(command, shell=shell, workdir=str(tmp_path), _bypass_gate=True))
    stopped = json.loads(exec_tools.exec_stop(started["command_id"], signal="kill"))
    polled = json.loads(exec_tools.exec_poll(started["command_id"]))

    assert stopped["status"] in {"ok", "error"}
    assert polled["reason_code"] == "unknown_command"


def test_completed_session_output_remains_readable(monkeypatch, tmp_path):
    _allow_exec(monkeypatch)
    shell, command = _python_command("print('read-twice')")
    started = json.loads(exec_tools.exec_start(command, shell=shell, workdir=str(tmp_path), _bypass_gate=True))

    for _ in range(30):
        first = json.loads(exec_tools.exec_poll(started["command_id"], return_mode="full"))
        if first.get("exit_code") is not None:
            break
        time.sleep(0.05)
    second = json.loads(exec_tools.exec_poll(started["command_id"], return_mode="full"))

    assert first["stdout"].strip() == "read-twice"
    assert second["stdout"].strip() == "read-twice"


def test_exec_write_stdin_rechecks_command_policy(monkeypatch, tmp_path):
    _allow_exec(monkeypatch)
    shell, command = _python_command("import time; time.sleep(5)")
    started = json.loads(exec_tools.exec_start(command, shell=shell, workdir=str(tmp_path), _bypass_gate=True))

    blocked = json.loads(exec_tools.exec_write_stdin(started["command_id"], "format.com E:\n"))
    exec_tools.exec_stop(started["command_id"], signal="kill")

    assert blocked["status"] == "blocked"
    assert blocked["reason_code"] == "command_blocked"


def test_exec_start_blocks_enforce_mode_sessions(monkeypatch, tmp_path):
    _allow_exec(monkeypatch)
    settings = AgentSettings()
    settings.sandbox.mode = "enforce"
    monkeypatch.setattr(exec_tools, "_ACTIVE_SETTINGS", type("Settings", (), {"sandbox": settings.sandbox})())

    result = json.loads(exec_tools.exec_start("echo hello", shell="bash", workdir=str(tmp_path)))

    assert result["status"] == "blocked"
    assert result["reason_code"] == "sandbox_sessions_unsupported"


def test_exec_start_blocks_when_policy_selects_docker(monkeypatch, tmp_path):
    _allow_exec(monkeypatch)

    def fake_decision(**_kwargs):
        return SandboxDecision(
            allowed=True,
            required=True,
            profile="untrusted",
            backend="docker",
            mode="auto",
            security_label="strong",
            network="deny",
            network_enforcement="enforced",
            write_strategy="copy_out",
            reason="fake docker decision",
        )

    class ExplodingRegistry:
        def start_local_direct(self, _request):
            raise AssertionError("docker policy must not start local direct")

        def start_local_restricted(self, _request):
            raise AssertionError("docker policy must not start local restricted")

    monkeypatch.setattr(exec_tools, "build_sandbox_decision", fake_decision)
    monkeypatch.setattr(exec_tools, "_SESSION_REGISTRY", ExplodingRegistry())

    result = json.loads(exec_tools.exec_start("npm install", shell="bash", workdir=str(tmp_path)))

    assert result["status"] == "blocked"
    assert result["reason_code"] == "sandbox_sessions_unsupported"
    assert result["sandbox"]["selected_backend"] == "docker"


def test_stale_session_cleanup():
    registry = SandboxSessionRegistry()
    shell, command = _python_command("import time; time.sleep(5)")
    handle = registry.start_local_direct(_session_request(command, shell))

    removed = registry.cleanup_stale(max_age_seconds=0)

    assert handle.session_id in removed
    assert registry.poll(handle.session_id).reason_code == "unknown_command"


def test_session_lifetime_expires():
    registry = SandboxSessionRegistry()
    shell, command = _python_command("import time; time.sleep(5)")
    request = _session_request(command, shell)
    request.resources = {"session_max_age_seconds": 1}
    handle = registry.start_local_direct(request)
    registry._sessions[handle.session_id]["started_at"] -= 2

    expired = registry.poll(handle.session_id)

    assert expired.reason_code == "session_expired"
