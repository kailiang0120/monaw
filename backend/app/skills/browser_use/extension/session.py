"""Per-tab DevTools state and the stable page ids shown to the model."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from .bridge import ExtensionBridge, ExtensionError

CONSOLE_LIMIT = 1000
# Console messages from this many recent navigations are kept.
CONSOLE_NAVIGATIONS_KEPT = 3


class ToolError(RuntimeError):
    """A tool failure the model can act on; the message is shown as is."""


@dataclass
class ConsoleMessage:
    msgid: int
    type: str
    text: str
    navigation: int
    stack: str = ""


@dataclass
class SnapshotNode:
    uid: str
    loader_id: str
    backend_node_id: int
    role: str
    name: str


@dataclass
class TabSession:
    bridge: ExtensionBridge
    tab_id: int
    prepared: bool = False
    main_frame_id: str = ""
    loader_id: str = ""
    navigation: int = 0
    load_count: int = 0
    navigation_started: int = 0
    # Main-frame loads that stopped, and URL changes without a new document
    # (history.pushState, #fragments). Neither fires a load event.
    loading_stopped: int = 0
    same_document: int = 0
    dialog: dict[str, Any] | None = None
    console: list[ConsoleMessage] = field(default_factory=list)
    next_msgid: int = 1
    snapshot_id: int = 0
    uid_counter: int = 0
    # loaderId_backendNodeId -> uid, so a node keeps its uid across snapshots
    # of the same document.
    uid_by_key: dict[str, str] = field(default_factory=dict)
    nodes: dict[str, SnapshotNode] = field(default_factory=dict)

    async def send(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
        await self.prepare()
        return await self._raw(method, params, timeout=timeout)

    async def _raw(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
        try:
            result = await self.bridge.request(
                "cdp", {"tabId": self.tab_id, "method": method, "params": params or {}}, timeout=timeout,
            )
        except ExtensionError as exc:
            if exc.code == "chrome_error":
                raise ToolError(_friendly_chrome_error(str(exc))) from exc
            raise
        return result if isinstance(result, dict) else {}

    async def prepare(self) -> None:
        if self.prepared:
            return
        await self._raw("Page.enable")
        await self._raw("Runtime.enable")
        tree = await self._raw("Page.getFrameTree")
        frame = (tree.get("frameTree") or {}).get("frame") or {}
        self.main_frame_id = str(frame.get("id") or "")
        self.loader_id = str(frame.get("loaderId") or "")
        self.prepared = True

    async def send_until_dialog(self, method: str, params: dict[str, Any] | None = None,
                                *, timeout: float = 30.0) -> bool:
        """Send a command that may open a JavaScript dialog.

        Chrome holds the reply to an input event until a dialog it opened is
        closed, so stop waiting as soon as a dialog appears. Returns whether a
        dialog opened.
        """
        await self.prepare()
        task = asyncio.ensure_future(self._raw(method, params, timeout=timeout))
        deadline = time.monotonic() + timeout
        while not task.done():
            if self.dialog is not None:
                task.add_done_callback(_ignore_result)
                return True
            if time.monotonic() > deadline:
                break
            await asyncio.sleep(0.03)
        await task
        return self.dialog is not None

    def on_event(self, method: str, params: dict[str, Any]) -> None:
        if method == "Page.frameNavigated":
            frame = params.get("frame") or {}
            if not frame.get("parentId"):
                self.main_frame_id = str(frame.get("id") or self.main_frame_id)
                self.loader_id = str(frame.get("loaderId") or "")
                self.navigation += 1
                self._trim_console()
        elif method in {"Page.frameStartedLoading", "Page.frameRequestedNavigation"}:
            if params.get("frameId") == self.main_frame_id:
                self.navigation_started += 1
        elif method == "Page.frameStoppedLoading":
            if params.get("frameId") == self.main_frame_id:
                self.loading_stopped += 1
        elif method == "Page.navigatedWithinDocument":
            if params.get("frameId") == self.main_frame_id:
                self.same_document += 1
        elif method == "Page.loadEventFired":
            self.load_count += 1
        elif method == "Page.javascriptDialogOpening":
            self.dialog = dict(params)
        elif method == "Page.javascriptDialogClosed":
            self.dialog = None
        elif method == "Runtime.consoleAPICalled":
            text = " ".join(_remote_object_text(arg) for arg in params.get("args") or [])
            self._add_console(str(params.get("type") or "log"), text, _stack_text(params.get("stackTrace")))
        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails") or {}
            exception = details.get("exception") or {}
            text = str(exception.get("description") or details.get("text") or "Uncaught exception")
            self._add_console("error", text, _stack_text(details.get("stackTrace")))

    def on_detached(self) -> None:
        self.prepared = False
        self.dialog = None

    def _add_console(self, kind: str, text: str, stack: str) -> None:
        self.console.append(ConsoleMessage(self.next_msgid, kind, text[:4000], self.navigation, stack))
        self.next_msgid += 1
        if len(self.console) > CONSOLE_LIMIT:
            del self.console[: len(self.console) - CONSOLE_LIMIT]

    def _trim_console(self) -> None:
        oldest = self.navigation - CONSOLE_NAVIGATIONS_KEPT
        self.console = [message for message in self.console if message.navigation > oldest]

    async def wait_for_load(self, loads_before: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.load_count > loads_before or self.dialog is not None:
                return True
            await asyncio.sleep(0.05)
        return False

    async def settle_after_action(self, *, timeout: float = 10.0) -> None:
        """Give a click or key press time to start a navigation, then wait for it to finish."""
        started, loads = self.navigation_started, self.load_count
        stopped, same_document = self.loading_stopped, self.same_document
        await asyncio.sleep(0.15)
        if self.navigation_started <= started or self.dialog is not None:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (self.load_count > loads or self.loading_stopped > stopped
                    or self.same_document > same_document or self.dialog is not None):
                return
            await asyncio.sleep(0.05)


def _ignore_result(task: asyncio.Future) -> None:
    if not task.cancelled():
        task.exception()


def _friendly_chrome_error(message: str) -> str:
    lowered = message.lower()
    if "cannot access" in lowered or "cannot attach" in lowered:
        return "Chrome does not allow automation on this page (for example chrome:// pages or the Web Store)."
    if "another debugger" in lowered:
        return "Another debugger (such as open DevTools) is attached to this tab. Close it and retry."
    if "no tab with" in lowered:
        return "That tab was closed. Call list_pages."
    return message


def _remote_object_text(arg: dict[str, Any]) -> str:
    if "value" in arg:
        value = arg["value"]
        return value if isinstance(value, str) else str(value)
    return str(arg.get("description") or arg.get("unserializableValue") or arg.get("type") or "")


def _stack_text(stack: Any) -> str:
    frames = (stack or {}).get("callFrames") if isinstance(stack, dict) else None
    lines = []
    for frame in (frames or [])[:10]:
        name = frame.get("functionName") or "(anonymous)"
        lines.append(f"    at {name} ({frame.get('url', '')}:{int(frame.get('lineNumber', 0)) + 1}:"
                     f"{int(frame.get('columnNumber', 0)) + 1})")
    return "\n".join(lines)


@dataclass
class TabInfo:
    tab_id: int
    url: str
    title: str
    active: bool
    focused_window: bool


class PageRegistry:
    """Maps Chrome tab ids to the small page ids the tools use."""

    def __init__(self, bridge: ExtensionBridge) -> None:
        self.bridge = bridge
        self.sessions: dict[int, TabSession] = {}
        self._page_by_tab: dict[int, int] = {}
        self._tab_by_page: dict[int, int] = {}
        self._next_page_id = 1
        self.selected_page: int | None = None
        bridge.add_listener(self._on_bridge_event)

    def _on_bridge_event(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        if kind == "disconnected":
            for session in self.sessions.values():
                session.on_detached()
            return
        tab_id = event.get("tabId")
        if not isinstance(tab_id, int):
            return
        if kind == "cdp":
            session = self.sessions.get(tab_id)
            if session is not None:
                session.on_event(str(event.get("method") or ""), event.get("params") or {})
        elif kind == "detached":
            session = self.sessions.get(tab_id)
            if session is not None:
                session.on_detached()
        elif kind == "tab.removed":
            self._forget(tab_id)

    def _forget(self, tab_id: int) -> None:
        self.sessions.pop(tab_id, None)
        page_id = self._page_by_tab.pop(tab_id, None)
        if page_id is not None:
            self._tab_by_page.pop(page_id, None)
            if self.selected_page == page_id:
                self.selected_page = None

    def page_id_for(self, tab_id: int) -> int:
        page_id = self._page_by_tab.get(tab_id)
        if page_id is None:
            page_id = self._next_page_id
            self._next_page_id += 1
            self._page_by_tab[tab_id] = page_id
            self._tab_by_page[page_id] = tab_id
        return page_id

    def session(self, tab_id: int) -> TabSession:
        session = self.sessions.get(tab_id)
        if session is None:
            session = TabSession(self.bridge, tab_id)
            self.sessions[tab_id] = session
        return session

    async def list_tabs(self) -> list[TabInfo]:
        raw = await self.bridge.request("tabs.list")
        tabs = [
            TabInfo(int(tab["id"]), str(tab.get("url") or ""), str(tab.get("title") or ""),
                    bool(tab.get("active")), bool(tab.get("focusedWindow")))
            for tab in raw or []
            if isinstance(tab, dict) and isinstance(tab.get("id"), int)
        ]
        live = {tab.tab_id for tab in tabs}
        for tab_id in list(self._page_by_tab):
            if tab_id not in live:
                self._forget(tab_id)
        for tab in tabs:
            self.page_id_for(tab.tab_id)
        return sorted(tabs, key=lambda tab: self._page_by_tab[tab.tab_id])

    async def resolve(self, page_id: int | None) -> tuple[int, TabSession]:
        tabs = await self.list_tabs()
        if page_id is None:
            page_id = self.selected_page
        if page_id is None or page_id not in self._tab_by_page:
            if page_id is not None:
                raise ToolError(f"No page with id {page_id}. Call list_pages to see open pages.")
            current = next((tab for tab in tabs if tab.active and tab.focused_window), None) or next(
                (tab for tab in tabs if tab.active), None
            )
            if current is None:
                raise ToolError("No open pages. Use new_page to open one.")
            page_id = self.page_id_for(current.tab_id)
        self.selected_page = page_id
        tab_id = self._tab_by_page[page_id]
        return page_id, self.session(tab_id)

    def tab_for_page(self, page_id: int) -> int | None:
        return self._tab_by_page.get(page_id)

    async def format_pages(self) -> str:
        tabs = await self.list_tabs()
        if self.selected_page not in self._tab_by_page:
            current = next((tab for tab in tabs if tab.active and tab.focused_window), None)
            self.selected_page = self.page_id_for(current.tab_id) if current else None
        lines = ["## Pages"]
        for tab in tabs:
            page_id = self._page_by_tab[tab.tab_id]
            title = tab.title.strip()
            if len(title) > 80:
                title = title[:77] + "..."
            label = f"{title} ({tab.url})" if title and title != tab.url else tab.url
            lines.append(f"{page_id}: {label}{' [selected]' if page_id == self.selected_page else ''}")
        if len(lines) == 1:
            lines.append("No open pages.")
        return "\n".join(lines)
