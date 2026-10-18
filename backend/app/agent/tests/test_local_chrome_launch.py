"""Local Chrome profile preparation for Chrome DevTools MCP, without touching a real Chrome."""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.settings_store import LLMSettings, _normalize_llm_payload
from app.skills.browser_use import chrome_launch as module


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
    assert module.read_system_cdp_url(str(tmp_path)) == expected


def test_lists_local_chrome_profiles_with_display_names(monkeypatch, tmp_path):
    for directory, name in (("Default", "Personal"), ("Profile 1", "Work")):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "Preferences").write_text(json.dumps({"profile": {"name": name}}), encoding="utf-8")
    (tmp_path / "Profile 2").mkdir()  # no Preferences: not a usable profile
    (tmp_path / "Crashpad").mkdir()
    monkeypatch.setattr(module, "system_chrome_user_data_dir", lambda: str(tmp_path))

    assert module.list_local_chrome_profiles() == [
        {"name": "Personal", "directory": "Default"},
        {"name": "Work", "directory": "Profile 1"},
    ]


def test_profile_listing_is_empty_without_chrome_data(monkeypatch):
    monkeypatch.setattr(module, "system_chrome_user_data_dir", lambda: "")
    assert module.list_local_chrome_profiles() == []


def test_last_used_profile_comes_from_local_state(tmp_path):
    (tmp_path / "Local State").write_text(json.dumps({"profile": {"last_used": "Profile 3"}}), encoding="utf-8")
    assert module.last_used_chrome_profile(str(tmp_path)) == "Profile 3"
    assert module.last_used_chrome_profile(str(tmp_path / "missing")) == ""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows install locations")
def test_finds_chrome_in_standard_windows_locations_before_registry(monkeypatch, tmp_path):
    chrome = tmp_path / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe"
    chrome.parent.mkdir(parents=True)
    chrome.write_bytes(b"")
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432"):
        monkeypatch.setenv(variable, str(tmp_path / "missing"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(module, "_windows_registry_chrome_paths", lambda: pytest.fail("registry is the fallback"))

    assert module.find_chrome_executable() == str(chrome)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows App Paths registry")
def test_falls_back_to_app_paths_registry_then_path(monkeypatch, tmp_path):
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "LOCALAPPDATA"):
        monkeypatch.setenv(variable, str(tmp_path / "missing"))
    registered = tmp_path / "Custom" / "chrome.exe"
    registered.parent.mkdir()
    registered.write_bytes(b"")
    monkeypatch.setattr(module, "_windows_registry_chrome_paths", lambda: [registered])
    assert module.find_chrome_executable() == str(registered)

    monkeypatch.setattr(module, "_windows_registry_chrome_paths", lambda: [tmp_path / "gone.exe"])
    monkeypatch.setattr(module.shutil, "which", lambda name: "C:/bin/chrome.exe" if name == "chrome" else None)
    assert module.find_chrome_executable() == "C:/bin/chrome.exe"

    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    assert module.find_chrome_executable() == ""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows App Paths registry")
def test_registry_lookup_reads_app_paths_default_value(monkeypatch):
    import winreg

    opened = []

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def open_key(hive, path):
        opened.append((hive, path))
        if hive == winreg.HKEY_CURRENT_USER:
            raise OSError("not per-user")
        return Key()

    monkeypatch.setattr(winreg, "OpenKey", open_key)
    monkeypatch.setattr(winreg, "QueryValueEx", lambda key, name: ('"C:\\Chrome\\chrome.exe"', 1))

    assert module._windows_registry_chrome_paths() == [Path("C:\\Chrome\\chrome.exe")]
    assert [path for _hive, path in opened] == [module._WINDOWS_APP_PATHS_KEY] * 2


def configure_launch(monkeypatch, tmp_path):
    (tmp_path / "Profile 1").mkdir()
    (tmp_path / "Profile 1" / "Preferences").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(module, "system_chrome_user_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(module, "find_chrome_executable", lambda: "chrome.exe")
    # Never consult the real machine's Chrome processes.
    monkeypatch.setattr(module, "_system_chrome_running", lambda root: False)
    launches = []
    monkeypatch.setattr(module, "_launch_personal_chrome", lambda args: launches.append(args))
    monkeypatch.setattr(module, "_local_debugging_endpoint_alive", lambda endpoint: True)
    return launches


def test_reuses_live_local_profile_without_opening_duplicate_windows(monkeypatch, tmp_path):
    launches = configure_launch(monkeypatch, tmp_path)
    (tmp_path / "DevToolsActivePort").write_text("12345\n/devtools/browser/abc", encoding="utf-8")

    endpoint, profile = asyncio.run(module.launch_local_chrome("Profile 1"))

    assert (endpoint, profile) == ("ws://127.0.0.1:12345/devtools/browser/abc", "Profile 1")
    assert launches == []


def test_defaults_to_chromes_last_used_profile(monkeypatch, tmp_path):
    configure_launch(monkeypatch, tmp_path)
    (tmp_path / "Local State").write_text(
        json.dumps({"profile": {"last_used": "Profile 1"}, "devtools": {"remote_debugging": {"user-enabled": True}}}),
        encoding="utf-8",
    )
    (tmp_path / "DevToolsActivePort").write_text("12345\n/devtools/browser/abc", encoding="utf-8")

    assert asyncio.run(module.launch_local_chrome())[1] == "Profile 1"


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
    launches = configure_launch(monkeypatch, tmp_path)
    (tmp_path / "DevToolsActivePort").write_text("9222\n/devtools/browser/stale", encoding="utf-8")
    monkeypatch.setattr(module, "_local_debugging_endpoint_alive", lambda endpoint: False)
    times = iter([0.0, 21.0])
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(times)))

    with pytest.raises(module.BrowserLaunchError) as error:
        asyncio.run(module.launch_local_chrome("Profile 1"))

    assert error.value.reason_code == "system_debugging_not_enabled"
    assert launches[0][0] == "chrome.exe"
    assert launches[0][-1] == "about:blank"
    assert f"--user-data-dir={tmp_path}" in launches[0]
    assert "--profile-directory=Profile 1" in launches[0]
    assert "--enable-features=DevToolsAcceptDebuggingConnections" in launches[0]
    assert "--start-maximized" in launches[0]
    state = json.loads((tmp_path / "Local State").read_text(encoding="utf-8"))
    assert state["devtools"]["remote_debugging"]["user-enabled"] is True


def test_first_time_setup_preserves_unrelated_profile_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "_system_chrome_running", lambda root: False)
    path = tmp_path / "Local State"
    path.write_text(json.dumps({"profile": {"last_used": "Profile 1"}, "other": {"value": 123}}), encoding="utf-8")
    module.prepare_local_chrome_debugging(str(tmp_path))
    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["profile"]["last_used"] == "Profile 1"
    assert state["other"] == {"value": 123}
    assert state["devtools"]["remote_debugging"]["user-enabled"] is True


def test_does_not_overwrite_running_chrome_preferences(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "_system_chrome_running", lambda root: True)
    path = tmp_path / "Local State"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(module.BrowserLaunchError) as error:
        module.prepare_local_chrome_debugging(str(tmp_path))
    assert error.value.reason_code == "chrome_setup_requires_restart"
    assert path.read_text(encoding="utf-8") == "{}"


def test_already_enabled_profile_is_not_rewritten_even_while_chrome_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "_system_chrome_running", lambda root: pytest.fail("no process check needed"))
    path = tmp_path / "Local State"
    original = json.dumps({"devtools": {"remote_debugging": {"user-enabled": True}}})
    path.write_text(original, encoding="utf-8")
    module.prepare_local_chrome_debugging(str(tmp_path))
    assert path.read_text(encoding="utf-8") == original


def test_invalid_profile_never_launches(monkeypatch, tmp_path):
    launches = configure_launch(monkeypatch, tmp_path)
    with pytest.raises(module.BrowserLaunchError) as error:
        asyncio.run(module.launch_local_chrome("../Other"))
    assert error.value.reason_code == "chrome_profile_missing"
    assert launches == []


def test_missing_chrome_is_reported(monkeypatch, tmp_path):
    configure_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "find_chrome_executable", lambda: "")
    with pytest.raises(module.BrowserLaunchError) as error:
        asyncio.run(module.launch_local_chrome("Profile 1"))
    assert error.value.reason_code == "chrome_executable_missing"


@pytest.mark.parametrize("cmdline,expected", [
    (["chrome.exe", "--user-data-dir=C:/Data"], "C:/Data"),
    (["chrome.exe", "--user-data-dir", '"C:/Other"'], "C:/Other"),
    (["chrome.exe"], ""),
])
def test_extracts_user_data_dir_from_chrome_command_line(cmdline, expected):
    assert module._extract_user_data_dir_from_cmdline(cmdline) == expected
