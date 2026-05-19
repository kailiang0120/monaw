import json
import os
import shlex
import sys
import time
from types import SimpleNamespace

from app.agent.sandbox.environment import build_exec_environment, is_secret_like_env_key
from app.skills.exec import tools as exec_tools


def _allow_exec(monkeypatch):
    monkeypatch.setattr(
        exec_tools,
        "resolve_permission",
        lambda *_args, **_kwargs: SimpleNamespace(
            blocked=False,
            requires_access_grant=False,
            requires_confirmation=False,
            reason="",
            reason_code="allowed",
            policy_source="settings.json",
        ),
    )


def _python_env_command(key: str) -> tuple[str, str]:
    code = f"import os; print(os.getenv({key!r}) or '')"
    if os.name == "nt":
        return "powershell", f'& "{sys.executable}" -c "{code}"'
    return "bash", f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def test_secret_like_env_keys_detect_common_patterns():
    assert is_secret_like_env_key("OPENAI_API_KEY")
    assert is_secret_like_env_key("GITHUB_TOKEN")
    assert is_secret_like_env_key("AWS_SECRET_ACCESS_KEY")
    assert is_secret_like_env_key("MY_PASSWORD")
    assert not is_secret_like_env_key("MONAW_TEST_VALUE")


def test_build_exec_environment_strips_host_secrets():
    env = build_exec_environment(
        requested_env={},
        shell="powershell",
        inherit_from={
            "PATH": r"C:\Windows\System32",
            "OPENAI_API_KEY": "should-not-leak",
            "TAVILY_API_KEY": "should-not-leak",
        },
    )

    assert not env.blocked
    assert "PATH" in env.env
    assert "OPENAI_API_KEY" not in env.env
    assert "TAVILY_API_KEY" not in env.env
    assert "OPENAI_API_KEY" not in env.inherited_keys


def test_build_exec_environment_allows_benign_explicit_env():
    env = build_exec_environment(
        requested_env={"MONAW_TEST_VALUE": "hello"},
        shell="powershell",
        inherit_from={"PATH": r"C:\Windows\System32"},
    )

    assert not env.blocked
    assert env.env["MONAW_TEST_VALUE"] == "hello"
    assert "MONAW_TEST_VALUE" in env.explicit_keys


def test_build_exec_environment_docker_does_not_inherit_host_env():
    env = build_exec_environment(
        requested_env={},
        shell="bash",
        backend="docker",
        inherit_from={
            "PATH": r"C:\Windows\System32",
            "HOME": r"C:\Users\example",
            "LANG": "en_US.UTF-8",
        },
    )

    assert not env.blocked
    assert env.env == {}
    assert env.inherited_keys == []


def test_build_exec_environment_docker_allows_benign_explicit_env():
    env = build_exec_environment(
        requested_env={"MONAW_TEST_VALUE": "hello"},
        shell="bash",
        backend="docker",
        inherit_from={"PATH": r"C:\Windows\System32"},
    )

    assert not env.blocked
    assert env.env == {"MONAW_TEST_VALUE": "hello"}
    assert env.explicit_keys == ["MONAW_TEST_VALUE"]


def test_build_exec_environment_blocks_explicit_secret_env():
    env = build_exec_environment(
        requested_env={"OPENAI_API_KEY": "abc"},
        shell="powershell",
        inherit_from={"PATH": r"C:\Windows\System32"},
    )

    assert env.blocked
    assert env.reason_code == "secret_env_not_supported"
    assert "OPENAI_API_KEY" in env.blocked_keys


def test_exec_tool_does_not_leak_host_secret_env(monkeypatch, tmp_path):
    _allow_exec(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-leak")
    shell, command = _python_env_command("OPENAI_API_KEY")

    result = json.loads(exec_tools.exec_tool(command, shell=shell, workdir=str(tmp_path)))

    assert result["status"] == "ok"
    assert "should-not-leak" not in result["stdout"]
    assert result["stdout"].strip() == ""
    assert result["sandbox"]["env_inheritance"] == "scrubbed"
    assert result["sandbox"]["backend"] in {"local_direct", "local_restricted", "docker"}


def test_exec_tool_allows_explicit_benign_env(monkeypatch, tmp_path):
    _allow_exec(monkeypatch)
    shell, command = _python_env_command("MONAW_TEST_VALUE")

    result = json.loads(
        exec_tools.exec_tool(
            command,
            shell=shell,
            workdir=str(tmp_path),
            env={"MONAW_TEST_VALUE": "hello"},
        )
    )

    assert result["status"] == "ok"
    assert result["stdout"].strip() == "hello"
    assert result["env_keys"] == ["MONAW_TEST_VALUE"]
    assert result["sandbox"]["explicit_env_keys"] == ["MONAW_TEST_VALUE"]


def test_exec_tool_blocks_explicit_secret_env(monkeypatch, tmp_path):
    _allow_exec(monkeypatch)
    shell, command = _python_env_command("OPENAI_API_KEY")

    result = json.loads(
        exec_tools.exec_tool(
            command,
            shell=shell,
            workdir=str(tmp_path),
            env={"OPENAI_API_KEY": "abc"},
        )
    )

    assert result["status"] == "blocked"
    assert result["reason_code"] == "secret_env_not_supported"
    assert result["blocked_env_keys"] == ["OPENAI_API_KEY"]
    assert result["sandbox"]["blocked_env_keys"] == ["OPENAI_API_KEY"]


def test_exec_start_uses_sanitized_env(monkeypatch, tmp_path):
    _allow_exec(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-leak")
    shell, command = _python_env_command("OPENAI_API_KEY")

    started = json.loads(exec_tools.exec_start(command, shell=shell, workdir=str(tmp_path)))
    assert started["status"] == "running"
    assert started["sandbox"]["env_inheritance"] == "scrubbed"

    command_id = started["command_id"]
    for _ in range(20):
        polled = json.loads(exec_tools.exec_poll(command_id, return_mode="full"))
        if polled.get("exit_code") is not None:
            break
        time.sleep(0.05)

    assert polled["status"] == "ok"
    assert "should-not-leak" not in polled["stdout"]
    assert polled["stdout"].strip() == ""
    assert polled["sandbox"]["backend"] in {"local_direct", "local_restricted"}


def test_bypass_gate_does_not_bypass_env_sanitization(monkeypatch, tmp_path):
    _allow_exec(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-leak")
    shell, command = _python_env_command("OPENAI_API_KEY")

    stripped = json.loads(
        exec_tools.exec_tool(command, shell=shell, workdir=str(tmp_path), _bypass_gate=True)
    )
    blocked = json.loads(
        exec_tools.exec_tool(
            command,
            shell=shell,
            workdir=str(tmp_path),
            env={"OPENAI_API_KEY": "abc"},
            _bypass_gate=True,
        )
    )

    assert stripped["status"] == "ok"
    assert "should-not-leak" not in stripped["stdout"]
    assert blocked["status"] == "blocked"
    assert blocked["reason_code"] == "secret_env_not_supported"
