import asyncio
import sys
import threading
from types import SimpleNamespace

import pytest

from app.skills.browser_use import chrome_approval as module


class Control:
    def __init__(self, name, kind="Pane", parent=None):
        self.name, self.kind, self.owner = name, kind, parent
        self.children = []
        self.element_info = SimpleNamespace(control_type=kind)
        if parent:
            parent.children.append(self)

    def window_text(self):
        return self.name

    def parent(self):
        return self.owner

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def descendants(self, **criteria):
        all_children = []
        for child in self.children:
            all_children.extend([child, *child.descendants()])
        return [child for child in all_children
                if (not criteria.get("control_type") or child.kind == criteria["control_type"])
                and (not criteria.get("title") or child.name == criteria["title"])]


def dialog(parent):
    box = Control("Allow remote debugging?", parent=parent)
    allow = Control("Allow", "Button", box)
    Control("Cancel", "Button", box)
    Control("Turn off in settings", "Button", box)
    return allow


def test_native_dialog_is_distinguished_from_identical_webpage_controls():
    window = Control("Chrome", "Window")
    document = Control("A webpage", "Document", window)
    fake = dialog(document)
    assert module._native_approval_button(window) == []
    genuine = dialog(window)
    assert module._native_approval_button(window) == [genuine]
    assert fake not in module._native_approval_button(window)


def test_owned_dialog_exposed_twice_counts_as_one_prompt(monkeypatch):
    button = SimpleNamespace(element_info=SimpleNamespace(runtime_id=[42, 1]))
    window = SimpleNamespace(class_name=lambda: "Chrome_WidgetWin_1")
    desktop = SimpleNamespace(windows=lambda **kwargs: [window, window])
    monkeypatch.setattr(module, "_native_approval_button", lambda window: [button])
    assert module._approval_candidates(desktop, 123) == [button]


@pytest.mark.parametrize("endpoint", [
    "ws://remote.example:9222/devtools/browser/test",
    "ws://127.0.0.1:9222/other", "wss://127.0.0.1/devtools/browser/test",
    "ws://someone@127.0.0.1/devtools/browser/test",
    "ws://127.0.0.1/devtools/browser/test?redirect=1",
])
def test_untrusted_endpoints_cannot_enable_approval(endpoint, monkeypatch):
    import psutil
    monkeypatch.setattr(psutil, "net_connections", lambda **kwargs: pytest.fail("Should not inspect processes"))
    assert module._endpoint_chrome_pid(endpoint) == 0


@pytest.mark.parametrize("existing,appearing,foreground_pid,expected", [
    (True, 1, 123, 0), (False, 2, 123, 0), (False, 1, 123, 1), (False, 1, 999, 0),
])
def test_watcher_leaves_existing_and_ambiguous_prompts_and_accepts_one_new_prompt(
    monkeypatch, existing, appearing, foreground_pid, expected,
):
    clicks = []
    button = SimpleNamespace(
        click_input=lambda: clicks.append("Allow"),
        top_level_parent=lambda: SimpleNamespace(set_focus=lambda: None),
        is_visible=lambda: True, is_enabled=lambda: True,
    )
    scans = iter([[button] if existing else [], [button] * appearing])
    monkeypatch.setattr(module, "_endpoint_chrome_pid", lambda endpoint: 123)
    monkeypatch.setattr(module, "_approval_candidates", lambda desktop, pid: next(scans))
    monkeypatch.setitem(sys.modules, "pythoncom", SimpleNamespace(CoInitialize=lambda: None, CoUninitialize=lambda: None))
    monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace(Desktop=lambda **kwargs: object()))
    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(GetForegroundWindow=lambda: 456))
    monkeypatch.setitem(sys.modules, "win32process", SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (1, foreground_pid)))
    ready = threading.Event()
    module._watch_approval("local", threading.Event(), ready)
    assert ready.is_set()
    assert len(clicks) == expected


def test_watcher_stops_when_connection_attempt_is_cancelled(monkeypatch):
    finished = threading.Event()
    def watch(endpoint, stopped, ready):
        ready.set()
        stopped.wait(2)
        if stopped.is_set():
            finished.set()
    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module, "_watch_approval", watch)
    async def scenario():
        entered = asyncio.Event()
        async def connect():
            async with module.approve_chrome_connection("local"):
                entered.set()
                await asyncio.sleep(999)
        task = asyncio.create_task(connect())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert finished.is_set()
