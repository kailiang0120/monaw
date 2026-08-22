import asyncio
import json
import subprocess

import pytest

from app.agent.sandbox import image_inventory
from app.agent.settings_store import AgentSettings
from app.api.routes import sandbox as sandbox_route
from app.schemas import SandboxResolveImagePayload


def _image(name: str = "python") -> str:
    return f"{name}@sha256:" + "a" * 64


def test_image_inventory_cache_is_bound_to_exact_digest(tmp_path):
    path = tmp_path / "inventories.json"
    modules = {"builtins", "os", "sys", "wsgiref"}

    image_inventory.store_python_module_inventory(_image(), modules, path=path)

    assert image_inventory.load_python_module_inventory(
        _image(), path=path
    ) == frozenset(modules)
    assert (
        image_inventory.load_python_module_inventory(
            "python@sha256:" + "b" * 64,
            path=path,
        )
        is None
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert payload["python_stdlib_modules"][_image()]["updated_at"]


def test_image_inventory_cache_rejects_unpinned_or_incomplete_data(tmp_path):
    path = tmp_path / "inventories.json"

    with pytest.raises(ValueError, match="immutable"):
        image_inventory.store_python_module_inventory(
            "python:3.12-slim", {"builtins", "os", "sys"}, path=path
        )
    with pytest.raises(ValueError, match="incomplete"):
        image_inventory.store_python_module_inventory(
            _image(), {"os", "sys"}, path=path
        )


def test_image_inventory_cache_ignores_corrupt_or_incomplete_entries(tmp_path):
    path = tmp_path / "inventories.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "python_stdlib_modules": {
                    _image(): {"modules": ["os", "sys"]},
                    "python:latest": {"modules": ["builtins", "os", "sys"]},
                },
            }
        ),
        encoding="utf-8",
    )

    assert image_inventory.load_python_module_inventory(_image(), path=path) is None


def test_image_inventory_cache_is_bounded_and_keeps_the_newest_images(tmp_path):
    path = tmp_path / "inventories.json"
    images = [f"python@sha256:{index:064x}" for index in range(12)]

    for image in images:
        image_inventory.store_python_module_inventory(
            image, {"builtins", "os", "sys"}, path=path
        )

    retained = json.loads(path.read_text(encoding="utf-8"))["python_stdlib_modules"]
    assert len(retained) == image_inventory._MAX_INVENTORY_ENTRIES
    # The image stored last is never the entry that gets evicted.
    assert images[-1] in retained
    assert images[0] not in retained
    assert image_inventory.load_python_module_inventory(images[0], path=path) is None
    assert image_inventory.load_python_module_inventory(images[-1], path=path) == frozenset(
        {"builtins", "os", "sys"}
    )


def test_python_inventory_probe_uses_hardened_container_and_validates_output(
    monkeypatch,
):
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            command, 0, '["builtins","os","sys","wsgiref"]\n', ""
        )

    monkeypatch.setattr(image_inventory.subprocess, "run", fake_run)

    modules = image_inventory.probe_python_module_inventory(_image())

    assert modules == frozenset({"builtins", "os", "sys", "wsgiref"})
    command = seen["command"]
    assert command[:2] == ["docker", "run"]
    assert ["--network", "none"] == command[
        command.index("--network") : command.index("--network") + 2
    ]
    assert ["--cap-drop", "ALL"] == command[
        command.index("--cap-drop") : command.index("--cap-drop") + 2
    ]
    assert "--read-only" in command
    assert ["--entrypoint", "python"] == command[
        command.index("--entrypoint") : command.index("--entrypoint") + 2
    ]
    assert _image() in command
    assert seen["kwargs"]["timeout"] == 180.0


def test_python_inventory_probe_fails_closed_on_incomplete_output(monkeypatch):
    monkeypatch.setattr(
        image_inventory.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, '["os","sys"]', ""
        ),
    )

    with pytest.raises(image_inventory.ImageInventoryProbeError, match="incomplete"):
        image_inventory.probe_python_module_inventory(_image())


def test_resolve_image_probes_and_persists_inventory(monkeypatch):
    resolved = _image("docker.io/library/python")
    settings = AgentSettings()
    stored = {}

    def fake_run(command, **_kwargs):
        if command[1] == "pull":
            return subprocess.CompletedProcess(command, 0, "pulled", "")
        return subprocess.CompletedProcess(command, 0, resolved + "\n", "")

    monkeypatch.setattr(sandbox_route.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(sandbox_route.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sandbox_route, "load_agent_settings", lambda _settings: settings
    )
    monkeypatch.setattr(
        sandbox_route,
        "save_agent_settings",
        lambda value: stored.setdefault("settings", value),
    )
    monkeypatch.setattr(
        sandbox_route,
        "probe_python_module_inventory",
        lambda image: frozenset({"builtins", "os", "sys", "wsgiref"}),
    )
    monkeypatch.setattr(
        sandbox_route,
        "store_python_module_inventory",
        lambda image, modules: stored.update(image=image, modules=modules),
    )
    monkeypatch.setattr(
        sandbox_route, "get_sandbox_status", lambda _settings: {"mode": "auto"}
    )

    response = asyncio.run(
        sandbox_route.resolve_docker_image(
            SandboxResolveImagePayload(image="python:3.12-slim")
        )
    )

    assert settings.sandbox.docker.image == resolved
    assert stored["image"] == resolved
    assert stored["modules"] == frozenset({"builtins", "os", "sys", "wsgiref"})
    assert "Verified 4 importable" in response["detail"]
