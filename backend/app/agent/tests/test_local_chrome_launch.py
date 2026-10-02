"""Local-profile launch and close recovery without touching a real Chrome process."""
import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.settings_store import LLMSettings, _normalize_llm_payload
from app.skills.browser_use import manager as module
from app.skills.browser_use import tools
from app.skills.browser_use import local_chrome_connection


def test_old_default_budgets_are_raised_but_custom_budgets_are_preserved():
    defaults = LLMSettings()
    migrated = _normalize_llm_payload({"max_iterations_per_turn": 40, "max_turn_seconds": 1800,
                                       "max_llm_call_seconds": 300})
    assert (defaults.max_iterations_per_turn, defaults.max_turn_seconds, defaults.max_llm_call_seconds) == (200, 7200, 600)
    assert migrated["max_iterations_per_turn"] == 200
    assert migrated["max_turn_seconds"] == 7200
    assert migrated["max_llm_call_seconds"] == 600
    custom = _normalize_llm_payload({"max_iterations_per_turn": 180, "max_turn_seconds": 1800,
                                    "max_llm_call_seconds": 300})
    assert custom["max_iterations_per_turn"] == 180
    assert custom["max_turn_seconds"] == 1800


@pytest.mark.parametrize("contents,expected", [
    ("12345\n/devtools/browser/abc\n", "ws://127.0.0.1:12345/devtools/browser/abc"),
    ("65536\n/devtools/browser/abc", ""),
    ("0\n/devtools/browser/abc", ""),
    ("12345\n//remote.example/abc", ""),
    ("12345\n/devtools/browser/abc?redirect=remote", ""),
    ("not-a-port", ""),
])
def test_discovers_only_valid_local_approved_endpoint(tmp_path, contents, expected):
    (tmp_path / "DevToolsActivePort").write_text(contents, encoding="utf-8")
    assert module._read_system_cdp_url(str(tmp_path)) == expected


def configure_launch(monkeypatch, tmp_path):
    (tmp_path / "Profile 1").mkdir()
    (tmp_path / "Profile 1" / "Preferences").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(module, "_system_chrome_user_data_dir", lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "browser_use.skill_cli.utils",
                        SimpleNamespace(find_chrome_executable=lambda: "chrome.exe"))
    launches = []
    monkeypatch.setattr(module, "_launch_personal_chrome", lambda args: launches.append(args))
    monkeypatch.setattr(module, "_local_debugging_endpoint_alive", lambda endpoint: True)
    monkeypatch.setattr(module, "_kill_existing_chrome_processes", lambda *args: pytest.fail("Launch must never kill Chrome"))
    monkeypatch.setattr(local_chrome_connection, "LocalChromeConnection", lambda: SimpleNamespace(
        start=AsyncMock(side_effect=lambda endpoint, **kwargs: endpoint), close=AsyncMock()))
    manager = module.BrowserUseManager({"system_profile_directory": "Profile 1"})
    return manager, launches


def test_reuses_live_local_profile_without_opening_duplicate_windows(monkeypatch, tmp_path):
    manager, launches = configure_launch(monkeypatch, tmp_path)
    (tmp_path / "DevToolsActivePort").write_text("12345\n/devtools/browser/abc", encoding="utf-8")
    manager._attach_system_locked = AsyncMock(return_value="connected")
    assert asyncio.run(manager._launch_system_locked()) == "connected"
    assert launches == []
    manager._attach_system_locked.assert_awaited_once_with("ws://127.0.0.1:12345/devtools/browser/abc", "Profile 1")
    assert manager._launched_chrome_proc is None
    assert manager._current_system_connection == "launch"


@pytest.mark.skipif(module.os.name != "nt", reason="Windows process tree lifecycle")
def test_windows_personal_chrome_launch_uses_a_finished_detached_bootstrap(monkeypatch):
    calls = []
    monkeypatch.setattr(module.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs)))
    args = ["chrome.exe", "--profile-directory=Default", "about:blank"]
    module._launch_personal_chrome(args)
    command, options = calls[0]
    assert command[:2] == [sys.executable, "-c"]
    assert command[3:] == args
    assert "DETACHED_PROCESS" in command[2]
    assert options["check"] is True
    assert options["timeout"] == 10
    assert options["creationflags"] == module.subprocess.CREATE_NO_WINDOW


def test_prepares_debugging_and_opens_normal_browser_not_setup_page(monkeypatch, tmp_path):
    manager, launches = configure_launch(monkeypatch, tmp_path)
    (tmp_path / "DevToolsActivePort").write_text("9222\n/devtools/browser/stale", encoding="utf-8")
    monkeypatch.setattr(module, "_local_debugging_endpoint_alive", lambda endpoint: False)
    times = iter([0.0, 21.0])
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(times)))
    with pytest.raises(module.BrowserLaunchError) as error:
        asyncio.run(manager._launch_system_locked())
    assert error.value.reason_code == "system_debugging_not_enabled"
    assert launches[0][-1] == "about:blank"
    assert "--enable-features=DevToolsAcceptDebuggingConnections" in launches[0]
    assert "--start-maximized" in launches[0]
    state = json.loads((tmp_path / "Local State").read_text(encoding="utf-8"))
    assert state["devtools"]["remote_debugging"]["user-enabled"] is True


def test_first_time_setup_preserves_unrelated_profile_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "_system_chrome_running", lambda root: False)
    path = tmp_path / "Local State"
    path.write_text(json.dumps({"profile": {"last_used": "Profile 1"}, "other": {"value": 123}}), encoding="utf-8")
    module._prepare_local_chrome_debugging(str(tmp_path))
    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["profile"]["last_used"] == "Profile 1"
    assert state["other"] == {"value": 123}


def test_does_not_overwrite_running_chrome_preferences(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "_system_chrome_running", lambda root: True)
    path = tmp_path / "Local State"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(module.BrowserLaunchError) as error:
        module._prepare_local_chrome_debugging(str(tmp_path))
    assert error.value.reason_code == "chrome_setup_requires_restart"
    assert path.read_text(encoding="utf-8") == "{}"


def test_page_tools_keep_one_off_browser_mode():
    manager = module.BrowserUseManager({"mode": "system"})
    manager._current_mode = "managed"
    manager.ensure_browser = AsyncMock(return_value="managed-session")
    assert asyncio.run(manager._existing_browser_or_start_default()) == "managed-session"
    manager.ensure_browser.assert_awaited_once_with("managed")


@pytest.mark.parametrize("fails", [False, True])
def test_default_browser_calls_honor_local_profile_without_managed_fallback(monkeypatch, fails):
    manager = module.BrowserUseManager({
        "mode": "system", "system_connection_strategy": "launch", "enable_system_fallback": True,
    })
    monkeypatch.setattr(module, "_browser_use_installed", lambda: True)
    manager._start_managed_locked = AsyncMock(side_effect=AssertionError("Must use the local profile"))
    manager._start_system_locked = AsyncMock(
        side_effect=module.BrowserLaunchError("Chrome needs approval", reason_code="system_debugging_approval_required")
        if fails else None,
        return_value="local-session",
    )
    if fails:
        with pytest.raises(module.BrowserLaunchError) as error:
            asyncio.run(manager.ensure_browser())
        assert error.value.reason_code == "system_debugging_approval_required"
    else:
        assert asyncio.run(manager.ensure_browser()) == "local-session"
    manager._start_system_locked.assert_awaited_once_with("")
    manager._start_managed_locked.assert_not_awaited()


def test_invalid_profile_never_launches(monkeypatch, tmp_path):
    manager, launches = configure_launch(monkeypatch, tmp_path)
    with pytest.raises(module.BrowserLaunchError) as error:
        asyncio.run(manager._launch_system_locked("../Other"))
    assert error.value.reason_code == "chrome_profile_missing"
    assert launches == []


def test_close_requires_approval_before_any_process_is_touched(monkeypatch):
    manager = SimpleNamespace(stop=AsyncMock())
    monkeypatch.setattr(tools, "current_interactive", lambda: True)
    monkeypatch.setattr(tools, "resolve_permission", lambda action: SimpleNamespace(
        blocked=False, requires_confirmation=True, reason="Process termination requires approval"))
    monkeypatch.setattr(tools, "create_ticket", lambda **args: SimpleNamespace(id="close-ticket", action_description=args["action_description"]))
    monkeypatch.setattr(tools, "_kill_existing_chrome_processes", lambda: pytest.fail("Approval is required"))
    result = json.loads(asyncio.run(tools.browser_close_chrome(manager)))
    assert result["status"] == "pending_approval"
    assert result["ticket_id"] == "close-ticket"
    manager.stop.assert_not_awaited()


def test_approved_close_disconnects_and_terminates_background_processes(monkeypatch):
    manager = SimpleNamespace(stop=AsyncMock())
    calls = []
    monkeypatch.setattr(tools, "_kill_existing_chrome_processes", lambda: calls.append("kill") or 3)
    result = json.loads(asyncio.run(tools.browser_close_chrome(manager, _bypass_gate=True)))
    manager.stop.assert_awaited_once()
    assert calls == ["kill"]
    assert result["processes_targeted"] == 3


@pytest.mark.parametrize("failure,reason", [
    (TimeoutError("timed out during opening handshake"), "system_debugging_approval_required"),
    (RuntimeError("Unsupported CDP method"), "system_attach_failed"),
])
def test_launch_reports_consent_separately_from_real_connection_errors(monkeypatch, tmp_path, failure, reason):
    manager, _launches = configure_launch(monkeypatch, tmp_path)
    (tmp_path / "DevToolsActivePort").write_text("12345\n/devtools/browser/abc", encoding="utf-8")
    connection = SimpleNamespace(start=AsyncMock(side_effect=failure), close=AsyncMock())
    monkeypatch.setattr(local_chrome_connection, "LocalChromeConnection", lambda: connection)
    with pytest.raises(module.BrowserLaunchError) as error:
        asyncio.run(manager._launch_system_locked())
    assert error.value.reason_code == reason
    connection.close.assert_awaited_once()
    assert manager._local_chrome_connection is None


def test_failed_library_start_disconnects_without_closing_personal_chrome(monkeypatch):
    browser = SimpleNamespace(start=AsyncMock(side_effect=RuntimeError("failed")), stop=AsyncMock())
    options = {}
    def create_browser(**kwargs):
        options.update(kwargs)
        return browser
    monkeypatch.setitem(sys.modules, "browser_use", SimpleNamespace(Browser=create_browser))
    monkeypatch.setitem(sys.modules, "browser_use.skill_cli.utils", SimpleNamespace(find_chrome_executable=lambda: "chrome.exe"))
    manager = module.BrowserUseManager({"keep_alive": False})
    manager._browser_kwargs = lambda: {"keep_alive": False}
    with pytest.raises(RuntimeError, match="failed"):
        asyncio.run(manager._attach_system_locked("ws://127.0.0.1:12345/test"))
    assert options["keep_alive"] is True
    browser.stop.assert_awaited_once()
    assert manager._browser is None


def test_relay_waits_for_consent_then_forwards_and_protects_the_connection():
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve
    from websockets.exceptions import InvalidStatus

    async def scenario():
        waiting, allowed = asyncio.Event(), asyncio.Event()
        async def approve(connection, request):
            waiting.set()
            await allowed.wait()
        async def chrome(client):
            async for message in client:
                await client.send(message)

        async with serve(chrome, "127.0.0.1", 0, process_request=approve) as server:
            chrome_endpoint = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            connection = local_chrome_connection.LocalChromeConnection()
            pending = asyncio.create_task(connection.start(chrome_endpoint))
            await asyncio.wait_for(waiting.wait(), 2)
            assert not pending.done()
            assert connection.endpoint == ""
            allowed.set()
            endpoint = await asyncio.wait_for(pending, 2)
            try:
                with pytest.raises(InvalidStatus) as error:
                    async with connect(endpoint + "wrong", proxy=None):
                        pass
                assert error.value.response.status_code == 404
                with pytest.raises(InvalidStatus) as error:
                    async with connect(endpoint, origin="https://untrusted.example", proxy=None):
                        pass
                assert error.value.response.status_code == 403
                async with connect(endpoint, proxy=None) as client:
                    query = '{"id":1,"method":"Target.getTargets"}'
                    await client.send(query)
                    assert await asyncio.wait_for(client.recv(), 2) == query
                    await client.send(b"binary")
                    assert await asyncio.wait_for(client.recv(), 2) == b"binary"
                    with pytest.raises(InvalidStatus) as error:
                        async with connect(endpoint, proxy=None):
                            pass
                    assert error.value.response.status_code == 409
            finally:
                await connection.close()
            # Disconnecting Monaw's relay leaves Chrome's server running.
            async with connect(chrome_endpoint, proxy=None) as other:
                await other.send("still running")
                assert await other.recv() == "still running"
    asyncio.run(scenario())
