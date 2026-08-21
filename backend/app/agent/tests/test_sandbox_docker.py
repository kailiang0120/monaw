import subprocess
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from app.agent.sandbox.backends import docker as docker_backend
from app.agent.sandbox.backends.docker import DockerRunner
from app.agent.sandbox.path_policy import collect_copy_out
from app.agent.sandbox.manager import SandboxManager
from app.agent.settings_store import AgentSettings

from .test_sandbox_manager import _capabilities, _request


def _docker_image_available(image: str) -> bool:
    try:
        version = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        image_check = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    return version.returncode == 0 and image_check.returncode == 0


def test_docker_command_defaults_to_hardened_flags():
    settings = AgentSettings()
    settings.sandbox.docker.image = "python@sha256:" + "a" * 64
    request = _request("python -c \"print('ok')\"")
    request.shell = "bash"

    command = DockerRunner(settings.sandbox).docker_command(request)

    assert command[:3] == ["docker", "run", "--rm"]
    assert ["--network", "none"] == command[command.index("--network") : command.index("--network") + 2]
    assert ["--cap-drop", "ALL"] == command[command.index("--cap-drop") : command.index("--cap-drop") + 2]
    assert "no-new-privileges" in command
    assert "--read-only" in command
    assert "--tmpfs" in command
    assert "--memory" in command
    assert "--cpus" in command
    assert "--pids-limit" in command
    assert command[-3:-1] == ["bash", "-lc"]


def test_docker_command_does_not_mount_host_paths_by_default():
    settings = AgentSettings()
    settings.sandbox.docker.image = "python@sha256:" + "a" * 64
    request = _request("echo ok")
    request.shell = "bash"
    command = DockerRunner(settings.sandbox).docker_command(request)

    assert "-v" not in command
    assert "--volume" not in command
    assert "--mount" not in command


def test_docker_command_honors_configured_pull_and_hardening_flags():
    settings = AgentSettings()
    settings.sandbox.docker.image = "python@sha256:" + "a" * 64
    settings.sandbox.docker.pull_policy = "never"
    settings.sandbox.docker.read_only_root = False
    settings.sandbox.docker.no_new_privileges = False
    request = _request("echo ok")
    request.shell = "bash"

    command = DockerRunner(settings.sandbox).docker_command(request)

    assert ["--pull", "never"] == command[command.index("--pull") : command.index("--pull") + 2]
    assert "--read-only" not in command
    assert "no-new-privileges" not in command


def test_docker_command_mounts_allowed_workspace_for_direct_rw(tmp_path):
    settings = AgentSettings()
    settings.sandbox.allowed_bind_roots = [str(tmp_path)]
    settings.sandbox.blocked_bind_roots = []
    settings.sandbox.default_write_strategy = "direct_rw"
    request = _request("echo ok")
    request.shell = "bash"
    request.workdir = str(tmp_path)
    request.copy_policy = {"write_strategy": "direct_rw"}
    runner = DockerRunner(settings.sandbox)
    mount_source, policy, copied = runner._workspace_mount(request, command_id="mount-test")

    command = runner.docker_command(request, mount_source=mount_source)

    assert mount_source == tmp_path.resolve()
    assert policy is None
    assert copied is False
    assert "--mount" in command
    assert f"source={tmp_path.resolve()}" in command[command.index("--mount") + 1]
    assert ",target=/workspace" in command[command.index("--mount") + 1]


def test_docker_copy_out_uses_a_run_workspace(tmp_path):
    settings = AgentSettings()
    settings.sandbox.allowed_bind_roots = [str(tmp_path)]
    settings.sandbox.blocked_bind_roots = []
    settings.sandbox.default_write_strategy = "copy_out"
    (tmp_path / "input.txt").write_text("input", encoding="utf-8")
    request = _request("echo ok")
    request.shell = "bash"
    request.workdir = str(tmp_path)
    request.copy_policy = {"write_strategy": "copy_out"}
    runner = DockerRunner(settings.sandbox)

    mount_source, policy, copied = runner._workspace_mount(request, command_id="copy-test")
    try:
        assert mount_source is not None
        assert policy is not None
        assert copied is True
        assert (mount_source / "input.txt").read_text(encoding="utf-8") == "input"
    finally:
        if mount_source is not None:
            import shutil

            shutil.rmtree(mount_source, ignore_errors=True)


def test_docker_copy_in_is_bounded_and_skips_symlink_entries(tmp_path):
    settings = AgentSettings()
    settings.sandbox.allowed_bind_roots = [str(tmp_path)]
    settings.sandbox.blocked_bind_roots = []
    settings.sandbox.default_write_strategy = "copy_out"
    (tmp_path / "large.bin").write_bytes(b"12345")
    request = _request("echo ok")
    request.shell = "bash"
    request.workdir = str(tmp_path)
    request.copy_policy = {"write_strategy": "copy_out"}
    request.resources = {"max_copy_in_bytes": 4}

    with pytest.raises(PermissionError, match="copy-in size limit"):
        DockerRunner(settings.sandbox)._workspace_mount(request, command_id="copy-limit")


def test_docker_copy_out_copies_only_changed_or_new_files(tmp_path):
    settings = AgentSettings()
    settings.sandbox.allowed_bind_roots = [str(tmp_path)]
    settings.sandbox.blocked_bind_roots = []
    settings.sandbox.default_write_strategy = "copy_out"
    existing = tmp_path / "existing.txt"
    existing.write_text("before", encoding="utf-8")
    request = _request("echo ok")
    request.shell = "bash"
    request.workdir = str(tmp_path)
    request.copy_policy = {"write_strategy": "copy_out"}
    runner = DockerRunner(settings.sandbox)

    mount_source, policy, copied = runner._workspace_mount(request, command_id="copy-diff")
    assert mount_source is not None and policy is not None and copied is True
    try:
        (mount_source / "existing.txt").write_text("after", encoding="utf-8")
        (mount_source / "new.txt").write_text("new", encoding="utf-8")
        artifacts = collect_copy_out(mount_source, tmp_path, policy)
        assert {Path(item["destination"]).name for item in artifacts} == {"existing.txt", "new.txt"}
        assert existing.read_text(encoding="utf-8") == "after"
    finally:
        import shutil

        shutil.rmtree(mount_source, ignore_errors=True)


def test_copy_out_size_limit_does_not_leave_partial_destination_changes(tmp_path):
    settings = AgentSettings()
    settings.sandbox.allowed_bind_roots = [str(tmp_path)]
    settings.sandbox.blocked_bind_roots = []
    settings.sandbox.default_write_strategy = "copy_out"
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("before-one", encoding="utf-8")
    second.write_text("before-two", encoding="utf-8")
    request = _request("echo ok")
    request.shell = "bash"
    request.workdir = str(tmp_path)
    request.copy_policy = {"write_strategy": "copy_out"}
    runner = DockerRunner(settings.sandbox)

    mount_source, policy, copied = runner._workspace_mount(request, command_id="copy-atomic")
    assert mount_source is not None and policy is not None and copied is True
    try:
        (mount_source / "first.txt").write_text("after-one", encoding="utf-8")
        (mount_source / "second.txt").write_text("after-two", encoding="utf-8")
        policy.max_copy_out_bytes = len("after-one") + len("after-two") - 1

        with pytest.raises(PermissionError, match="copy-out size limit"):
            collect_copy_out(mount_source, tmp_path, policy)

        assert first.read_text(encoding="utf-8") == "before-one"
        assert second.read_text(encoding="utf-8") == "before-two"
    finally:
        import shutil

        shutil.rmtree(mount_source, ignore_errors=True)


def test_docker_result_warns_about_ephemeral_container_filesystem(monkeypatch, tmp_path):
    settings = AgentSettings()
    settings.sandbox.allowed_bind_roots = [str(tmp_path)]
    settings.sandbox.blocked_bind_roots = []
    settings.sandbox.docker.image = "python@sha256:" + "a" * 64
    request = _request("echo ok")
    request.shell = "bash"
    request.workdir = str(tmp_path)
    request.copy_policy = {"write_strategy": "discard"}
    runner = DockerRunner(settings.sandbox)

    monkeypatch.setattr(runner, "is_available", lambda: True)
    monkeypatch.setattr(
        docker_backend,
        "_run_command_capped",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout="ok\n", stderr="", returncode=0, timed_out=False, cancelled=False
        ),
    )

    result = runner.run(request)

    assert result.status == "ok"
    assert any("ephemeral" in warning for warning in result.sandbox["warnings"])


def test_auto_mode_selects_docker_when_strong_backend_is_available(monkeypatch):
    settings = AgentSettings()
    settings.sandbox.docker.image = "python@sha256:" + "a" * 64
    manager = SandboxManager(settings.sandbox, capabilities=_capabilities(docker=True, local=True))

    monkeypatch.setattr(manager.docker, "is_available", lambda: True)
    monkeypatch.setattr(manager.docker, "run", lambda request: manager._blocked_result(
        request,
        backend="docker",
        reason="fake docker selected",
        reason_code="fake_selected",
    ))

    request = _request("echo ok")
    request.shell = "bash"
    result = manager.run(request)

    assert result.status == "blocked"
    assert result.reason_code == "fake_selected"
    assert result.sandbox["selected_backend"] == "docker"


def test_enforce_mode_allows_docker_when_available(monkeypatch):
    settings = AgentSettings()
    settings.sandbox.docker.image = "python@sha256:" + "a" * 64
    settings.sandbox.mode = "enforce"
    manager = SandboxManager(settings.sandbox, capabilities=_capabilities(docker=True, local=True))

    monkeypatch.setattr(manager.docker, "is_available", lambda: True)
    monkeypatch.setattr(manager.docker, "run", lambda request: manager._blocked_result(
        request,
        backend="docker",
        reason="fake docker selected",
        reason_code="fake_selected",
    ))

    request = _request("echo ok")
    request.shell = "bash"
    result = manager.run(request)

    assert result.status == "blocked"
    assert result.reason_code == "fake_selected"
    assert result.sandbox["selected_backend"] == "docker"


def test_docker_runner_blocks_unsupported_shell():
    settings = AgentSettings()
    settings.sandbox.docker.image = "python@sha256:" + "a" * 64
    request = _request("Write-Output ok")
    request.backend = "docker"
    request.shell = "powershell"

    result = DockerRunner(settings.sandbox).run(request)

    assert result.status == "blocked"
    assert result.reason_code == "unsupported_shell_for_backend"
    assert "bash commands" in result.reason


def test_docker_timeout_removes_named_container(monkeypatch):
    settings = AgentSettings()
    settings.sandbox.docker.image = "python@sha256:" + "a" * 64
    runner = DockerRunner(settings.sandbox)
    request = _request("sleep 5")
    request.shell = "bash"
    request.timeout = 1
    removed: list[str] = []
    captured_name = ""

    monkeypatch.setattr(runner, "is_available", lambda: True)
    monkeypatch.setattr(runner, "_remove_container", lambda name: removed.append(name))

    def fake_run_command(command, _request, **kwargs):
        nonlocal captured_name
        captured_name = command[command.index("--name") + 1]
        kwargs["on_timeout"](SimpleNamespace())
        return SimpleNamespace(returncode=-9, stdout="", stderr="", timed_out=True, cancelled=False)

    monkeypatch.setattr(docker_backend, "_run_command_capped", fake_run_command)

    result = runner.run(request)

    assert result.status == "error"
    assert result.timed_out is True
    assert removed == [captured_name]
    assert captured_name.startswith("monaw-sandbox-")


@pytest.mark.skipif(
    not _docker_image_available(AgentSettings().sandbox.docker.image),
    reason="Docker daemon or configured sandbox image is unavailable",
)
def test_docker_integration_executes_simple_command_when_available(tmp_path):
    settings = AgentSettings()
    workdir = Path(tmp_path)
    settings.sandbox.allowed_bind_roots = [str(workdir)]
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "input.txt").write_text("mounted", encoding="utf-8")
    runner = DockerRunner(settings.sandbox)
    request = _request("python -c \"from pathlib import Path; print(Path('/workspace/input.txt').read_text()); Path('/workspace/output.txt').write_text('ok')\"")
    request.shell = "bash"
    request.workdir = str(workdir)
    request.copy_policy = {"write_strategy": "direct_rw"}
    result = runner.run(request)

    assert result.status == "ok"
    assert result.stdout.strip() == "mounted"
    assert (workdir / "output.txt").read_text(encoding="utf-8") == "ok"
    assert result.sandbox["backend"] == "docker"
    assert result.sandbox["security_label"] == "strong"
