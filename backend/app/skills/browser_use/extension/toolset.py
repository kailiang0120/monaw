"""Browser tools implemented over the extension's DevTools relay."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bridge import ExtensionBridge, ExtensionError
from .chrome import ensure_extension_connected
from .keys import key_events
from .session import PageRegistry, SnapshotNode, TabSession, ToolError
from .snapshot import take_snapshot
from .tools_schema import TOOL_NAMES

DEFAULT_NAVIGATION_TIMEOUT_S = 30.0
DEFAULT_WAIT_FOR_TIMEOUT_S = 15.0
MAX_SCRIPT_RESULT_CHARS = 20_000
CLOSE_PAGE_ERROR = "The last open page cannot be closed. It is fine to keep it open."
# These tools would hang while a JavaScript dialog blocks the page.
_DIALOG_BLOCKED = {
    "take_snapshot", "take_screenshot", "wait_for", "click", "hover", "drag", "fill", "fill_form",
    "type_text", "press_key", "upload_file", "evaluate_script",
}

_FILL_PREPARE = r"""function(value) {
  const el = this.nodeType === Node.TEXT_NODE ? this.parentElement : this;
  if (!el) return {kind: 'error', message: 'This element cannot be filled.'};
  el.scrollIntoView({block: 'center', inline: 'nearest'});
  if (el instanceof HTMLSelectElement) {
    const options = Array.from(el.options);
    const match = options.find(o => o.value === value) || options.find(o => o.textContent.trim() === value.trim());
    if (!match) {
      return {kind: 'error', message: `No option "${value}". Options: ` + options.map(o => o.textContent.trim()).slice(0, 40).join(', ')};
    }
    el.value = match.value;
    match.selected = true;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    return {kind: 'done'};
  }
  const role = (el.getAttribute && el.getAttribute('role')) || '';
  const isInput = el instanceof HTMLInputElement;
  const toggle = (isInput && (el.type === 'checkbox' || el.type === 'radio'))
    || ['checkbox', 'radio', 'switch', 'menuitemcheckbox', 'menuitemradio'].includes(role);
  if (toggle) {
    if (value !== 'true' && value !== 'false') {
      return {kind: 'error', message: 'Use "true" or "false" for checkboxes, radio buttons and switches.'};
    }
    const checked = isInput ? el.checked : el.getAttribute('aria-checked') === 'true';
    if (checked !== (value === 'true')) el.click();
    return {kind: 'done'};
  }
  if ((isInput || el instanceof HTMLTextAreaElement) && (el.readOnly || el.disabled)) {
    return {kind: 'error', message: 'The field is read-only or disabled.'};
  }
  if (isInput && ['date', 'time', 'datetime-local', 'month', 'week', 'color', 'range'].includes(el.type)) {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, value);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    return {kind: 'done'};
  }
  el.focus();
  if (isInput || el instanceof HTMLTextAreaElement) {
    try { el.select(); } catch (error) { el.value = ''; }
    return {kind: 'type'};
  }
  if (el.isContentEditable) {
    const range = document.createRange();
    range.selectNodeContents(el);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    return {kind: 'type'};
  }
  return {kind: 'error', message: 'This element cannot be filled. Click it, or fill a text field, select or checkbox.'};
}"""

# Custom radios and checkboxes (Apple's colour swatches, for example) hide the
# real <input> and draw its <label> instead. Clicking the label is what a
# person does, so use the label's centre when the control has no size.
_LABEL_CENTER = r"""function() {
  const el = this.nodeType === Node.TEXT_NODE ? this.parentElement : this;
  if (!el) return null;
  const labels = [];
  if (el.labels) labels.push(...el.labels);
  const enclosing = el.closest && el.closest('label');
  if (enclosing) labels.push(enclosing);
  const labelledBy = (el.getAttribute && el.getAttribute('aria-labelledby')) || '';
  for (const id of labelledBy.split(/\s+/).filter(Boolean)) {
    const target = document.getElementById(id);
    if (target) labels.push(target);
  }
  for (const label of labels) {
    label.scrollIntoView({block: 'center', inline: 'nearest'});
    const rect = label.getBoundingClientRect();
    if (rect.width > 1 && rect.height > 1) {
      return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
    }
  }
  return null;
}"""

# Last resort for a control with no size and no visible label.
_SCRIPT_CLICK = r"""function(double) {
  const el = this.nodeType === Node.TEXT_NODE ? this.parentElement : this;
  if (!el || typeof el.click !== 'function') return false;
  if (typeof el.focus === 'function') el.focus({preventScroll: true});
  el.click();
  if (double) {
    el.click();
    el.dispatchEvent(new MouseEvent('dblclick', {bubbles: true, cancelable: true, detail: 2}));
  }
  return true;
}"""

_SELECT_OPTION = r"""function() {
  const select = this.closest && this.closest('select');
  if (!select) return false;
  select.value = this.value;
  this.selected = true;
  select.dispatchEvent(new Event('input', {bubbles: true}));
  select.dispatchEvent(new Event('change', {bubbles: true}));
  return true;
}"""


class ElementNotVisible(ToolError):
    """The element has no box on screen (for example a visually hidden input)."""


@dataclass
class ToolOutput:
    lines: list[str] = field(default_factory=list)
    images: list[dict[str, str]] = field(default_factory=list)

    def add(self, line: str) -> None:
        if line:
            self.lines.append(line)


def _timeout_seconds(value: Any, default: float) -> float:
    try:
        milliseconds = int(value or 0)
    except (TypeError, ValueError):
        return default
    return milliseconds / 1000.0 if milliseconds > 0 else default


def _page_id(arguments: dict[str, Any]) -> int | None:
    value = arguments.get("pageId")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ToolError(f"pageId must be a number from list_pages, not {value!r}.") from None


class BrowserTools:
    def __init__(self, bridge: ExtensionBridge) -> None:
        self.bridge = bridge
        self.pages = PageRegistry(bridge)

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolOutput:
        if name not in TOOL_NAMES:
            raise ToolError(f"Unknown tool {name}.")
        await ensure_extension_connected(self.bridge)
        handler = getattr(self, f"_tool_{name}")
        try:
            return await handler(arguments)
        except ExtensionError as exc:
            if exc.code in {"extension_disconnected", "extension_not_connected"}:
                raise ToolError("The Monaw Chrome extension disconnected during the call. Retry once Chrome is open.") from exc
            raise ToolError(str(exc)) from exc

    # -- helpers -------------------------------------------------------------

    async def _page(self, arguments: dict[str, Any], tool: str) -> tuple[int, TabSession]:
        page_id, session = await self.pages.resolve(_page_id(arguments))
        await session.prepare()
        if tool in _DIALOG_BLOCKED and session.dialog is not None:
            raise ToolError(self._dialog_message(session))
        return page_id, session

    @staticmethod
    def _dialog_message(session: TabSession) -> str:
        dialog = session.dialog or {}
        return (
            f'A JavaScript {dialog.get("type") or ""} dialog is open on this page: "{dialog.get("message", "")}". '
            "Use handle_dialog to accept or dismiss it."
        )

    def _node(self, session: TabSession, uid: str) -> SnapshotNode:
        if not session.nodes:
            raise ToolError("No snapshot found for this page. Use take_snapshot to capture one.")
        node = session.nodes.get(str(uid))
        if node is None:
            raise ToolError(f"Element uid {uid} was not found in the latest snapshot. Take a new snapshot.")
        if node.loader_id != session.loader_id:
            raise ToolError(f"Element uid {uid} is from before the page navigated. Take a new snapshot.")
        return node

    async def _object_id(self, session: TabSession, node: SnapshotNode) -> str:
        try:
            result = await session.send("DOM.resolveNode", {"backendNodeId": node.backend_node_id})
        except ToolError as exc:
            raise ToolError(f"Element uid {node.uid} no longer exists on the page. Take a new snapshot.") from exc
        object_id = (result.get("object") or {}).get("objectId")
        if not object_id:
            raise ToolError(f"Element uid {node.uid} no longer exists on the page. Take a new snapshot.")
        return str(object_id)

    async def _call_on(self, session: TabSession, node: SnapshotNode, function: str, *args: Any) -> Any:
        object_id = await self._object_id(session, node)
        result = await session.send("Runtime.callFunctionOn", {
            "objectId": object_id,
            "functionDeclaration": function,
            "arguments": [{"value": arg} for arg in args],
            "returnByValue": True,
            "awaitPromise": True,
        })
        if result.get("exceptionDetails"):
            raise ToolError(_exception_text(result["exceptionDetails"]))
        return (result.get("result") or {}).get("value")

    async def _center(self, session: TabSession, node: SnapshotNode) -> tuple[float, float]:
        """Where to point the mouse: the element, or its label when the element is hidden."""
        try:
            await session.send("DOM.scrollIntoViewIfNeeded", {"backendNodeId": node.backend_node_id})
        except ToolError:
            pass
        try:
            box = await self._box(session, node)
        except ElementNotVisible:
            point = await self._call_on(session, node, _LABEL_CENTER)
            if isinstance(point, dict) and "x" in point and "y" in point:
                return float(point["x"]), float(point["y"])
            raise
        return box[0] + box[2] / 2, box[1] + box[3] / 2

    async def _box(self, session: TabSession, node: SnapshotNode) -> tuple[float, float, float, float]:
        try:
            result = await session.send("DOM.getContentQuads", {"backendNodeId": node.backend_node_id})
        except ToolError as exc:
            raise ToolError(f"Element uid {node.uid} no longer exists on the page. Take a new snapshot.") from exc
        best: tuple[float, float, float, float] | None = None
        for quad in result.get("quads") or []:
            xs, ys = quad[0::2], quad[1::2]
            box = (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
            if box[2] * box[3] > 1 and (best is None or box[2] * box[3] > best[2] * best[3]):
                best = box
        if best is None:
            raise ElementNotVisible(f"Element uid {node.uid} is not visible (it has no size on screen). Take a new snapshot.")
        return best

    async def _mouse(self, session: TabSession, kind: str, x: float, y: float, **extra: Any) -> bool:
        params = {"type": kind, "x": x, "y": y, **extra}
        if kind == "mouseMoved":
            await session.send("Input.dispatchMouseEvent", params)
            return False
        return await session.send_until_dialog("Input.dispatchMouseEvent", params)

    async def _click_point(self, session: TabSession, x: float, y: float, count: int) -> bool:
        await self._mouse(session, "mouseMoved", x, y)
        for click in range(1, count + 1):
            if await self._mouse(session, "mousePressed", x, y, button="left", buttons=1, clickCount=click):
                return True
            if await self._mouse(session, "mouseReleased", x, y, button="left", buttons=0, clickCount=click):
                return True
        return False

    async def _press(self, session: TabSession, combo: str) -> bool:
        for event in key_events(combo):
            if event["type"] == "keyUp":
                await session.send("Input.dispatchKeyEvent", event)
            elif await session.send_until_dialog("Input.dispatchKeyEvent", event):
                return True
        return False

    async def _insert_text(self, session: TabSession, text: str) -> bool:
        if not text:
            return False
        return await session.send_until_dialog("Input.insertText", {"text": text})

    async def _with_snapshot(self, output: ToolOutput, session: TabSession, include: bool) -> ToolOutput:
        if include and session.dialog is None:
            output.add(await take_snapshot(session))
        return output

    async def _wait_for_navigation(self, session: TabSession, loads: int, navigations: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        next_ready_check = 0.0
        while time.monotonic() < deadline:
            if session.load_count > loads or session.dialog is not None:
                return True
            # Pages restored from the back/forward cache fire no load event.
            if session.navigation > navigations and time.monotonic() >= next_ready_check:
                next_ready_check = time.monotonic() + 0.5
                try:
                    state = await session.send("Runtime.evaluate", {
                        "expression": "document.readyState", "returnByValue": True,
                    }, timeout=2.0)
                    if (state.get("result") or {}).get("value") == "complete":
                        return True
                except (ToolError, ExtensionError):
                    pass
            await asyncio.sleep(0.05)
        return False

    async def _run_navigation(self, session: TabSession, method: str, params: dict[str, Any],
                              timeout: float, before_unload: str) -> tuple[dict[str, Any], bool, bool]:
        """Run a navigation command; returns (result, loaded, beforeunload handled)."""
        loads, navigations = session.load_count, session.navigation
        task = asyncio.ensure_future(session.send(method, params, timeout=timeout))
        handled = False
        while not task.done():
            dialog = session.dialog
            if dialog is not None and dialog.get("type") == "beforeunload" and not handled:
                await session.send("Page.handleJavaScriptDialog", {"accept": before_unload != "dismiss"})
                session.dialog = None
                handled = True
            await asyncio.sleep(0.03)
        result = await task
        if result.get("errorText"):
            return result, False, handled
        if method == "Page.navigate" and not result.get("loaderId"):
            return result, True, handled  # Same-document navigation, such as a #fragment.
        loaded = await self._wait_for_navigation(session, loads, navigations, timeout)
        return result, loaded, handled

    # -- pages ---------------------------------------------------------------

    async def _tool_list_pages(self, arguments: dict[str, Any]) -> ToolOutput:
        return ToolOutput([await self.pages.format_pages()])

    async def _tool_select_page(self, arguments: dict[str, Any]) -> ToolOutput:
        page_id, session = await self.pages.resolve(_page_id(arguments))
        if arguments.get("bringToFront"):
            await self.bridge.request("tabs.activate", {"tabId": session.tab_id})
        return ToolOutput([await self.pages.format_pages()])

    async def _tool_new_page(self, arguments: dict[str, Any]) -> ToolOutput:
        url = str(arguments.get("url") or "").strip()
        if not url:
            raise ToolError("A URL is required.")
        await self.pages.list_tabs()  # Existing tabs keep the lower page ids.
        tab = await self.bridge.request("tabs.create", {"active": not arguments.get("background")})
        tab_id = int((tab or {}).get("id"))
        page_id = self.pages.page_id_for(tab_id)
        self.pages.selected_page = page_id
        session = self.pages.session(tab_id)
        await session.prepare()
        timeout = _timeout_seconds(arguments.get("timeout"), DEFAULT_NAVIGATION_TIMEOUT_S)
        result, loaded, _handled = await self._run_navigation(session, "Page.navigate", {"url": url}, timeout, "accept")
        output = ToolOutput()
        if result.get("errorText"):
            output.add(f"Unable to load {url}: {result['errorText']}.")
        elif not loaded:
            output.add(f"Opened {url}; the page is still loading.")
        output.add(await self.pages.format_pages())
        return output

    async def _tool_navigate_page(self, arguments: dict[str, Any]) -> ToolOutput:
        page_id, session = await self._page(arguments, "navigate_page")
        kind = arguments.get("type") or ("url" if arguments.get("url") else "")
        if not kind:
            raise ToolError("Either URL or a type is required.")
        timeout = _timeout_seconds(arguments.get("timeout"), DEFAULT_NAVIGATION_TIMEOUT_S)
        before_unload = str(arguments.get("handleBeforeUnload") or "accept")
        output = ToolOutput()
        if kind == "url":
            url = str(arguments.get("url") or "").strip()
            if not url:
                raise ToolError("A URL is required for navigation of type=url.")
            result, loaded, handled = await self._run_navigation(session, "Page.navigate", {"url": url}, timeout, before_unload)
            if result.get("errorText"):
                output.add(f"Unable to navigate in the selected page: {result['errorText']}.")
            else:
                output.add(f"Successfully navigated to {url}." + ("" if loaded else " The page is still loading."))
        elif kind == "reload":
            _result, loaded, handled = await self._run_navigation(
                session, "Page.reload", {"ignoreCache": bool(arguments.get("ignoreCache"))}, timeout, before_unload,
            )
            output.add("Successfully reloaded the page." + ("" if loaded else " The page is still loading."))
        elif kind in {"back", "forward"}:
            history = await session.send("Page.getNavigationHistory")
            entries = history.get("entries") or []
            index = int(history.get("currentIndex") or 0) + (-1 if kind == "back" else 1)
            if not 0 <= index < len(entries):
                output.add(f"Unable to navigate {kind} in the selected page: there is no {'previous' if kind == 'back' else 'next'} page.")
                handled = False
            else:
                _result, loaded, handled = await self._run_navigation(
                    session, "Page.navigateToHistoryEntry", {"entryId": entries[index]["id"]}, timeout, before_unload,
                )
                output.add(f"Successfully navigated {kind} to {entries[index].get('url', '')}."
                           + ("" if loaded else " The page is still loading."))
        else:
            raise ToolError(f"Unknown navigation type {kind}.")
        if handled:
            output.add(f"{'Dismissed' if before_unload == 'dismiss' else 'Accepted'} a beforeunload dialog.")
        if session.dialog is not None:
            output.add(self._dialog_message(session))
        output.add(await self.pages.format_pages())
        return output

    async def _tool_close_page(self, arguments: dict[str, Any]) -> ToolOutput:
        tabs = await self.pages.list_tabs()
        page_id = _page_id(arguments)
        tab_id = self.pages.tab_for_page(page_id) if page_id is not None else None
        if tab_id is None:
            raise ToolError(f"No page with id {page_id}. Call list_pages to see open pages.")
        output = ToolOutput()
        if len(tabs) <= 1:
            output.add(CLOSE_PAGE_ERROR)
        else:
            await self.bridge.request("tabs.close", {"tabId": tab_id})
        output.add(await self.pages.format_pages())
        return output

    # -- reading -------------------------------------------------------------

    async def _tool_take_snapshot(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "take_snapshot")
        return ToolOutput([await take_snapshot(session)])

    async def _tool_take_screenshot(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "take_screenshot")
        image_format = str(arguments.get("format") or "png")
        if arguments.get("uid") and arguments.get("fullPage"):
            raise ToolError("uid and fullPage cannot be used together.")
        # Chrome only paints the visible tab, so show it first.
        await self.bridge.request("tabs.activate", {"tabId": session.tab_id})
        params: dict[str, Any] = {"format": image_format}
        if image_format != "png" and arguments.get("quality") is not None:
            params["quality"] = int(arguments["quality"])
        if arguments.get("uid"):
            node = self._node(session, str(arguments["uid"]))
            await self._center(session, node)
            x, y, width, height = await self._box(session, node)
            metrics = await session.send("Page.getLayoutMetrics")
            viewport = metrics.get("cssVisualViewport") or {}
            params["clip"] = {
                "x": x + float(viewport.get("pageX") or 0), "y": y + float(viewport.get("pageY") or 0),
                "width": width, "height": height, "scale": 1,
            }
            params["captureBeyondViewport"] = True
        elif arguments.get("fullPage"):
            metrics = await session.send("Page.getLayoutMetrics")
            size = metrics.get("cssContentSize") or {}
            params["clip"] = {
                "x": 0, "y": 0, "width": float(size.get("width") or 1), "height": float(size.get("height") or 1), "scale": 1,
            }
            params["captureBeyondViewport"] = True
        result = await session.send("Page.captureScreenshot", params, timeout=45.0)
        data = str(result.get("data") or "")
        if not data:
            raise ToolError("Chrome returned an empty screenshot.")
        label = "the element" if arguments.get("uid") else ("the full page" if arguments.get("fullPage") else "the current page viewport")
        return ToolOutput([f"Took a screenshot of {label}."], [{"data": data, "mimeType": f"image/{image_format}"}])

    async def _tool_wait_for(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "wait_for")
        texts = [str(text) for text in arguments.get("text") or [] if str(text)]
        if not texts:
            raise ToolError("Provide at least one text to wait for.")
        timeout = _timeout_seconds(arguments.get("timeout"), DEFAULT_WAIT_FOR_TIMEOUT_S)
        deadline = time.monotonic() + timeout
        expression = "document.body ? document.body.innerText : ''"
        while True:
            if session.dialog is not None:
                raise ToolError(self._dialog_message(session))
            result = await session.send("Runtime.evaluate", {"expression": expression, "returnByValue": True}, timeout=5.0)
            body = str((result.get("result") or {}).get("value") or "")
            found = next((text for text in texts if text in body), None)
            if found is not None:
                return ToolOutput([f"Element matching one of {json.dumps(texts)} found.", await take_snapshot(session)])
            if time.monotonic() >= deadline:
                raise ToolError(f"Timed out after {int(timeout * 1000)} ms waiting for {json.dumps(texts)}.")
            await asyncio.sleep(0.25)

    async def _tool_list_console_messages(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "list_console_messages")
        messages = session.console
        if not arguments.get("includePreservedMessages"):
            messages = [message for message in messages if message.navigation == session.navigation]
        types = {str(kind) for kind in arguments.get("types") or []}
        if types:
            messages = [message for message in messages if message.type in types]
        size = arguments.get("pageSize")
        index = int(arguments.get("pageIdx") or 0)
        if size:
            messages = messages[index * int(size): (index + 1) * int(size)]
        lines = ["## Console messages"]
        lines += [f"msgid={message.msgid} [{message.type}] {message.text[:500]}" for message in messages]
        if len(lines) == 1:
            lines.append("<no console messages found>")
        return ToolOutput(["\n".join(lines)])

    async def _tool_get_console_message(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "get_console_message")
        msgid = int(arguments.get("msgid") or 0)
        message = next((item for item in session.console if item.msgid == msgid), None)
        if message is None:
            raise ToolError(f"No console message with msgid {msgid}. Call list_console_messages.")
        text = f"msgid={message.msgid} [{message.type}]\n{message.text}"
        if message.stack:
            text += f"\n### Stack trace\n{message.stack}"
        return ToolOutput([text])

    # -- input ---------------------------------------------------------------

    async def _tool_click(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "click")
        node = self._node(session, str(arguments.get("uid")))
        double = bool(arguments.get("dblClick"))
        output = ToolOutput()
        # A native <select> option cannot be clicked once its popup is drawn by Chrome.
        if node.role == "option" and await self._call_on(session, node, _SELECT_OPTION):
            output.add("Successfully clicked on the element")
            return await self._with_snapshot(output, session, bool(arguments.get("includeSnapshot")))
        note = ""
        try:
            x, y = await self._center(session, node)
            opened_dialog = await self._click_point(session, x, y, 2 if double else 1)
        except ElementNotVisible:
            opened_dialog = await self._script_click(session, node, double)
            note = " (it is hidden on screen, so it was clicked directly)"
        if opened_dialog:
            output.add("The element was clicked and it opened a dialog.")
            output.add(self._dialog_message(session))
            return output
        await session.settle_after_action()
        output.add(("Successfully double clicked on the element" if double else "Successfully clicked on the element") + note)
        return await self._with_snapshot(output, session, bool(arguments.get("includeSnapshot")))

    async def _script_click(self, session: TabSession, node: SnapshotNode, double: bool) -> bool:
        """Click with element.click(); returns whether a dialog opened."""
        object_id = await self._object_id(session, node)
        return await session.send_until_dialog("Runtime.callFunctionOn", {
            "objectId": object_id,
            "functionDeclaration": _SCRIPT_CLICK,
            "arguments": [{"value": double}],
            "returnByValue": True,
            "userGesture": True,
        })

    async def _tool_hover(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "hover")
        node = self._node(session, str(arguments.get("uid")))
        x, y = await self._center(session, node)
        await self._mouse(session, "mouseMoved", x, y)
        await asyncio.sleep(0.1)
        output = ToolOutput(["Successfully hovered over the element"])
        return await self._with_snapshot(output, session, bool(arguments.get("includeSnapshot")))

    async def _tool_drag(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "drag")
        source = self._node(session, str(arguments.get("from_uid")))
        target = self._node(session, str(arguments.get("to_uid")))
        start = await self._center(session, source)
        end = await self._center(session, target)
        await self._mouse(session, "mouseMoved", *start)
        await self._mouse(session, "mousePressed", *start, button="left", buttons=1, clickCount=1)
        steps = 8
        for step in range(1, steps + 1):
            x = start[0] + (end[0] - start[0]) * step / steps
            y = start[1] + (end[1] - start[1]) * step / steps
            await session.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "buttons": 1})
            await asyncio.sleep(0.02)
        await self._mouse(session, "mouseReleased", *end, button="left", buttons=0, clickCount=1)
        await session.settle_after_action()
        output = ToolOutput(["Successfully dragged an element"])
        return await self._with_snapshot(output, session, bool(arguments.get("includeSnapshot")))

    async def _fill_one(self, session: TabSession, uid: str, value: str) -> bool:
        """Fill one element; returns whether it opened a dialog."""
        node = self._node(session, uid)
        prepared = await self._call_on(session, node, _FILL_PREPARE, value) or {}
        kind = prepared.get("kind") if isinstance(prepared, dict) else None
        if kind == "error":
            raise ToolError(f"Could not fill uid {uid}: {prepared.get('message')}")
        if kind == "type":
            if value:
                return await self._insert_text(session, value)
            return await self._press(session, "Delete")
        return session.dialog is not None

    async def _tool_fill(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "fill")
        if await self._fill_one(session, str(arguments.get("uid")), str(arguments.get("value") or "")):
            return ToolOutput(["The element was filled out and it opened a dialog.", self._dialog_message(session)])
        output = ToolOutput(["Successfully filled out the element"])
        return await self._with_snapshot(output, session, bool(arguments.get("includeSnapshot")))

    async def _tool_fill_form(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "fill_form")
        elements = [item for item in arguments.get("elements") or [] if isinstance(item, dict)]
        if not elements:
            raise ToolError("Provide at least one element to fill.")
        for position, element in enumerate(elements):
            uid = str(element.get("uid"))
            if await self._fill_one(session, uid, str(element.get("value") or "")):
                remaining = len(elements) - position - 1
                return ToolOutput([
                    f"Filling out the element with uid {uid} opened a dialog."
                    + (f" {remaining} element(s) were not filled." if remaining else ""),
                    self._dialog_message(session),
                ])
        output = ToolOutput(["Successfully filled out the form"])
        return await self._with_snapshot(output, session, bool(arguments.get("includeSnapshot")))

    async def _tool_type_text(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "type_text")
        text = str(arguments.get("text") or "")
        submit = str(arguments.get("submitKey") or "")
        if await self._insert_text(session, text) or (submit and await self._press(session, submit)):
            return ToolOutput([f'Typed text "{text}" and it opened a dialog.', self._dialog_message(session)])
        if submit:
            await session.settle_after_action()
        return ToolOutput([f'Typed text "{text}{f" + {submit}" if submit else ""}"'])

    async def _tool_press_key(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "press_key")
        key = str(arguments.get("key") or "")
        if await self._press(session, key):
            return ToolOutput([f"Pressed key {key} and it opened a dialog.", self._dialog_message(session)])
        await session.settle_after_action()
        output = ToolOutput([f"Successfully pressed key: {key}"])
        return await self._with_snapshot(output, session, bool(arguments.get("includeSnapshot")))

    async def _tool_handle_dialog(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "handle_dialog")
        if session.dialog is None:
            raise ToolError("No open dialog found")
        accept = arguments.get("action") == "accept"
        params: dict[str, Any] = {"accept": accept}
        if accept and arguments.get("promptText") is not None:
            params["promptText"] = str(arguments["promptText"])
        try:
            await session.send("Page.handleJavaScriptDialog", params)
        except ToolError:
            pass  # Already closed by the user.
        session.dialog = None
        output = ToolOutput(["Successfully accepted the dialog" if accept else "Successfully dismissed the dialog"])
        output.add(await self.pages.format_pages())
        return output

    async def _tool_upload_file(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "upload_file")
        node = self._node(session, str(arguments.get("uid")))
        paths = [str(path) for path in arguments.get("filePaths") or []]
        missing = [path for path in paths if not Path(path).is_absolute() or not Path(path).is_file()]
        if missing:
            raise ToolError("These files do not exist or are not absolute paths: " + ", ".join(missing))
        try:
            await session.send("DOM.setFileInputFiles", {"files": paths, "backendNodeId": node.backend_node_id})
        except ToolError as exc:
            raise ToolError(
                f"Could not upload through uid {node.uid}: {exc}. Choose the page's <input type=file> element from the snapshot."
            ) from exc
        output = ToolOutput([f"File uploaded from {', '.join(paths)}."])
        return await self._with_snapshot(output, session, bool(arguments.get("includeSnapshot")))

    async def _tool_evaluate_script(self, arguments: dict[str, Any]) -> ToolOutput:
        _page_id_value, session = await self._page(arguments, "evaluate_script")
        function = str(arguments.get("function") or "").strip()
        if not function:
            raise ToolError("A function is required.")
        handles = [
            {"objectId": await self._object_id(session, self._node(session, str(uid)))}
            for uid in arguments.get("args") or []
        ]
        global_object = await session.send("Runtime.evaluate", {"expression": "globalThis"})
        object_id = (global_object.get("result") or {}).get("objectId")
        result = await session.send("Runtime.callFunctionOn", {
            "functionDeclaration": function,
            "objectId": object_id,
            "arguments": handles,
            "returnByValue": True,
            "awaitPromise": True,
            "userGesture": True,
        }, timeout=60.0)
        if result.get("exceptionDetails"):
            raise ToolError(f"Script failed: {_exception_text(result['exceptionDetails'])}")
        value = result.get("result") or {}
        if "value" in value:
            text = json.dumps(value["value"], ensure_ascii=False, indent=2)
        else:
            text = str(value.get("description") or value.get("type") or "undefined")
        if len(text) > MAX_SCRIPT_RESULT_CHARS:
            text = text[:MAX_SCRIPT_RESULT_CHARS] + "\n... (truncated)"
        return ToolOutput([f"Script ran on page and returned:\n```json\n{text}\n```"])


def _exception_text(details: dict[str, Any]) -> str:
    exception = details.get("exception") or {}
    return str(exception.get("description") or details.get("text") or "The script threw an exception.")
