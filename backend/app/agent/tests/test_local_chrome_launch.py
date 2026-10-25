"""Finding and opening the user's Chrome for the Monaw extension, without touching a real Chrome."""
import sys
from pathlib import Path

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


@pytest.mark.skipif(module.os.name != "nt", reason="Windows process tree lifecycle")
def test_windows_chrome_launch_uses_a_finished_detached_bootstrap(monkeypatch):
    calls = []
    monkeypatch.setattr(module.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs)))
    args = ["chrome.exe", "chrome://extensions/"]
    module.launch_chrome_detached(args)
    command, options = calls[0]
    assert command[:2] == [sys.executable, "-c"]
    assert command[3:] == args
    assert "DETACHED_PROCESS" in command[2]
    assert options["check"] is True
    assert options["timeout"] == 10
    assert options["creationflags"] == module.subprocess.CREATE_NO_WINDOW


@pytest.mark.parametrize("cmdline,expected", [
    (["chrome.exe", "--user-data-dir=C:/Data"], "C:/Data"),
    (["chrome.exe", "--user-data-dir", '"C:/Other"'], "C:/Other"),
    (["chrome.exe"], ""),
])
def test_extracts_user_data_dir_from_chrome_command_line(cmdline, expected):
    assert module._extract_user_data_dir_from_cmdline(cmdline) == expected
