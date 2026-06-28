import asyncio

import pytest

from app.main import _split_csv_setting
from app.skills.browser_use import manager as browser_manager_module
from app import main as main_module
from app.agent import scheduler as scheduler_module
from app import startup_security


def test_split_csv_setting_drops_empty_cors_entries():
    assert _split_csv_setting(" file://, null ,, http://localhost:5173 ") == [
        "file://",
        "null",
        "http://localhost:5173",
    ]


def test_lifespan_does_not_prewarm_browser(monkeypatch):
    runtime_calls: list[str] = []
    browser_calls: list[str] = []
    scheduler_events: list[str] = []

    monkeypatch.setattr(main_module, "ensure_browser_use_runtime_dirs", lambda: runtime_calls.append("dirs"))
    monkeypatch.setattr(main_module.settings, "telegram_bot_token", "")

    def fake_configure_browser_use_manager(_settings):
        browser_calls.append("configure")
        raise AssertionError("browser prewarm should not run during startup")

    monkeypatch.setattr(
        browser_manager_module,
        "configure_browser_use_manager",
        fake_configure_browser_use_manager,
    )

    class FakeScheduledTaskService:
        def __init__(self, *, settings_factory):
            self.settings_factory = settings_factory

        async def start(self):
            scheduler_events.append("start")

        async def stop(self):
            scheduler_events.append("stop")

    monkeypatch.setattr(scheduler_module, "ScheduledTaskService", FakeScheduledTaskService)

    async def run() -> None:
        async with main_module.lifespan(main_module.app):
            pass

    asyncio.run(run())

    assert runtime_calls == ["dirs"]
    assert browser_calls == []
    assert scheduler_events == ["start", "stop"]


def test_startup_security_requires_control_plane_secret(monkeypatch):
    monkeypatch.delenv("MONAW_CONTROL_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="control-plane secret"):
        startup_security.validate_control_plane_secret()


def test_startup_security_rejects_symlink_runtime_directory(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this platform")

    with pytest.raises(RuntimeError, match="symlink"):
        startup_security.validate_runtime_directory(linked)
