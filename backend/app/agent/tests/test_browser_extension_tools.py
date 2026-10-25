import asyncio

import pytest

from app.skills.browser_use.extension.keys import key_events, parse_key_combo
from app.skills.browser_use.extension.session import ToolError
from app.skills.browser_use.extension.toolset import BrowserTools
from app.skills.browser_use.extension.tools_schema import TOOL_DEFINITIONS, TOOL_NAMES
from app.skills.mcp_bridge.builtin import BUILTIN_TOOL_POLICIES, CHROME_DEVTOOLS_SERVER


def _ax(node_id, role, name="", children=(), backend=None, parent=None, ignored=False, properties=(), value=None):
    node = {
        "nodeId": str(node_id), "role": {"value": role}, "name": {"value": name}, "ignored": ignored,
        "childIds": [str(child) for child in children], "properties": list(properties),
    }
    if backend is not None:
        node["backendDOMNodeId"] = backend
    if parent is not None:
        node["parentId"] = str(parent)
    if value is not None:
        node["value"] = {"value": value}
    return node


PAGE_TREE = [
    _ax(1, "RootWebArea", "Example", children=[2], backend=1),
    _ax(2, "generic", children=[3, 4, 6, 7], parent=1, backend=2),
    _ax(3, "heading", "Welcome", parent=2, backend=10, properties=[{"name": "level", "value": {"value": 1}}]),
    _ax(4, "button", "Submit", children=[5], parent=2, backend=11,
        properties=[{"name": "focusable", "value": {"value": True}}]),
    _ax(5, "StaticText", "Submit", parent=4, backend=12),
    _ax(6, "textbox", "Email", parent=2, backend=13, value="a@b.c"),
    _ax(7, "none", parent=2, ignored=True, backend=14),
]


class FakeChrome:
    """Stands in for the extension: tabs plus a scripted DevTools page."""

    def __init__(self):
        self.connected = True
        self.listeners = []
        self.calls: list[tuple[str, dict]] = []
        self.tabs = [{"id": 7, "url": "https://example.com/", "title": "Example", "active": True, "focusedWindow": True}]
        self.loader = "L1"
        self.tree = list(PAGE_TREE)
        self.fill_kind = {"kind": "type"}
        self.open_dialog_on_release = False
        self.hidden_nodes: set[int] = set()
        self.label_point = None

    def add_listener(self, listener):
        self.listeners.append(listener)

    def emit(self, method, params, tab_id=7):
        for listener in self.listeners:
            listener({"type": "event", "event": "cdp", "tabId": tab_id, "method": method, "params": params})

    async def request(self, method, params=None, *, timeout=30.0):
        params = params or {}
        if method == "tabs.list":
            return list(self.tabs)
        if method == "tabs.create":
            tab = {"id": 8, "url": "about:blank", "title": "", "active": True, "focusedWindow": True}
            self.tabs.append(tab)
            return tab
        if method in {"tabs.activate", "tabs.close"}:
            self.calls.append((method, params))
            return True
        assert method == "cdp"
        cdp, args = params["method"], params["params"]
        self.calls.append((cdp, args))
        if cdp == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "F1", "loaderId": self.loader}}}
        if cdp == "Accessibility.getFullAXTree":
            return {"nodes": self.tree}
        if cdp == "DOM.getContentQuads":
            if args["backendNodeId"] in self.hidden_nodes:
                return {"quads": [[5, 5, 5, 5, 5, 5, 5, 5]]}  # Visually hidden: no area.
            return {"quads": [[10, 20, 110, 20, 110, 60, 10, 60]]}
        if cdp == "DOM.resolveNode":
            return {"object": {"objectId": f"obj-{args['backendNodeId']}"}}
        if cdp == "Runtime.callFunctionOn":
            function = args["functionDeclaration"]
            if "el.labels" in function:
                return {"result": {"value": self.label_point}}
            return {"result": {"value": self.fill_kind if "el.select" in function else 42}}
        if cdp == "Runtime.evaluate":
            if args["expression"] == "globalThis":
                return {"result": {"objectId": "global"}}
            return {"result": {"value": "Hello Welcome"}}
        if cdp == "Page.navigate":
            asyncio.get_running_loop().call_soon(self.emit, "Page.loadEventFired", {}, params["tabId"])
            return {"frameId": "F1", "loaderId": "L2"}
        if cdp == "Input.dispatchMouseEvent" and args["type"] == "mouseReleased" and self.open_dialog_on_release:
            self.emit("Page.javascriptDialogOpening", {"type": "alert", "message": "Hi"})
            await asyncio.sleep(5)  # Chrome holds the reply while the dialog is open.
        if cdp == "Page.captureScreenshot":
            return {"data": "aGVsbG8="}
        return {}


def _run(tools, name, **arguments):
    return asyncio.run(tools.call(name, arguments))


def _tools():
    chrome = FakeChrome()
    return BrowserTools(chrome), chrome


def test_tool_names_are_a_subset_of_the_policy_table():
    policies = BUILTIN_TOOL_POLICIES[CHROME_DEVTOOLS_SERVER]
    assert TOOL_NAMES <= set(policies)
    for tool in TOOL_DEFINITIONS:
        properties = tool["inputSchema"]["properties"]
        assert "filePath" not in properties and "initScript" not in properties
    upload = next(tool for tool in TOOL_DEFINITIONS if tool["name"] == "upload_file")
    assert "filePaths" in upload["inputSchema"]["required"]


def test_list_pages_uses_stable_page_ids():
    tools, chrome = _tools()
    output = _run(tools, "list_pages")
    assert output.lines == ["## Pages\n1: Example (https://example.com/) [selected]"]
    chrome.tabs.insert(0, {"id": 3, "url": "about:blank", "title": "", "active": False, "focusedWindow": True})
    text = _run(tools, "list_pages").lines[0]
    assert "1: Example (https://example.com/) [selected]" in text and "2: about:blank" in text


def test_snapshot_format_and_uid_stability():
    tools, chrome = _tools()
    first = _run(tools, "take_snapshot").lines[0]
    assert first.splitlines() == [
        "## Latest page snapshot",
        'uid=1_1 RootWebArea "Example"',
        '  uid=1_2 heading "Welcome" level="1"',
        '  uid=1_3 button "Submit" focusable',
        '  uid=1_5 textbox "Email" value="a@b.c"',
    ]
    second = _run(tools, "take_snapshot").lines[0]
    assert 'uid=1_3 button "Submit"' in second
    # A navigation changes the loader id, so old uids become stale.
    chrome.emit("Page.frameNavigated", {"frame": {"id": "F1", "loaderId": "L9"}})
    with pytest.raises(ToolError, match="before the page navigated"):
        _run(tools, "click", uid="1_3")
    assert "uid=3_" in _run(tools, "take_snapshot").lines[0]


def test_click_dispatches_mouse_events_at_the_element_center():
    tools, chrome = _tools()
    _run(tools, "take_snapshot")
    output = _run(tools, "click", uid="1_3")
    assert output.lines == ["Successfully clicked on the element"]
    mouse = [args for method, args in chrome.calls if method == "Input.dispatchMouseEvent"]
    assert [event["type"] for event in mouse] == ["mouseMoved", "mousePressed", "mouseReleased"]
    assert (mouse[1]["x"], mouse[1]["y"]) == (60, 40)


def test_click_that_opens_a_dialog_returns_without_waiting_and_blocks_reads():
    tools, chrome = _tools()
    _run(tools, "take_snapshot")
    chrome.open_dialog_on_release = True
    output = _run(tools, "click", uid="1_3")
    assert "opened a dialog" in output.lines[0]
    with pytest.raises(ToolError, match="handle_dialog"):
        _run(tools, "take_snapshot")
    assert _run(tools, "handle_dialog", action="accept").lines[0] == "Successfully accepted the dialog"
    assert ("Page.handleJavaScriptDialog", {"accept": True}) in chrome.calls


def test_fill_types_into_text_fields_and_reports_unfillable_elements():
    tools, chrome = _tools()
    _run(tools, "take_snapshot")
    assert _run(tools, "fill", uid="1_5", value="me@x.y").lines == ["Successfully filled out the element"]
    assert ("Input.insertText", {"text": "me@x.y"}) in chrome.calls
    chrome.fill_kind = {"kind": "error", "message": "This element cannot be filled."}
    with pytest.raises(ToolError, match="Could not fill uid 1_3"):
        _run(tools, "fill_form", elements=[{"uid": "1_3", "value": "x"}])


def test_unknown_uid_and_missing_snapshot_errors():
    tools, _chrome = _tools()
    with pytest.raises(ToolError, match="No snapshot found"):
        _run(tools, "click", uid="1_3")
    _run(tools, "take_snapshot")
    with pytest.raises(ToolError, match="was not found"):
        _run(tools, "hover", uid="9_9")


def test_navigate_waits_for_load_and_lists_pages():
    tools, chrome = _tools()
    output = _run(tools, "navigate_page", url="https://example.com/next")
    assert output.lines[0] == "Successfully navigated to https://example.com/next."
    assert output.lines[-1].startswith("## Pages")
    with pytest.raises(ToolError, match="Either URL or a type"):
        _run(tools, "navigate_page")


def test_new_page_opens_a_tab_and_selects_it():
    tools, chrome = _tools()
    output = _run(tools, "new_page", url="https://example.org/")
    assert "2: about:blank [selected]" in output.lines[-1]


def test_close_page_refuses_the_last_page():
    tools, chrome = _tools()
    _run(tools, "list_pages")
    output = _run(tools, "close_page", pageId=1)
    assert output.lines[0].startswith("The last open page cannot be closed")
    assert not any(method == "tabs.close" for method, _ in chrome.calls)


def test_screenshot_returns_an_image_block():
    tools, chrome = _tools()
    output = _run(tools, "take_screenshot")
    assert output.images == [{"data": "aGVsbG8=", "mimeType": "image/png"}]
    assert ("tabs.activate", {"tabId": 7}) in chrome.calls


def test_wait_for_and_evaluate_script():
    tools, chrome = _tools()
    assert "found" in _run(tools, "wait_for", text=["Welcome"]).lines[0]
    output = _run(tools, "evaluate_script", function="() => 42")
    assert output.lines == ["Script ran on page and returned:\n```json\n42\n```"]


def test_upload_requires_existing_absolute_files(tmp_path):
    tools, chrome = _tools()
    _run(tools, "take_snapshot")
    with pytest.raises(ToolError, match="do not exist"):
        _run(tools, "upload_file", uid="1_5", filePaths=["relative.txt"])
    upload = tmp_path / "a.txt"
    upload.write_text("x", encoding="utf-8")
    assert "File uploaded" in _run(tools, "upload_file", uid="1_5", filePaths=[str(upload)]).lines[0]
    assert ("DOM.setFileInputFiles", {"files": [str(upload)], "backendNodeId": 13}) in chrome.calls


def test_console_messages_are_buffered_per_navigation():
    tools, chrome = _tools()
    _run(tools, "list_pages")
    _run(tools, "take_snapshot")
    chrome.emit("Runtime.consoleAPICalled", {"type": "log", "args": [{"type": "string", "value": "hello"}]})
    chrome.emit("Runtime.exceptionThrown", {"exceptionDetails": {"exception": {"description": "TypeError: x"}}})
    text = _run(tools, "list_console_messages").lines[0]
    assert "msgid=1 [log] hello" in text and "msgid=2 [error] TypeError: x" in text
    assert "TypeError: x" in _run(tools, "get_console_message", msgid=2).lines[0]
    chrome.emit("Page.frameNavigated", {"frame": {"id": "F1", "loaderId": "L5"}})
    assert "<no console messages found>" in _run(tools, "list_console_messages").lines[0]
    assert "hello" in _run(tools, "list_console_messages", includePreservedMessages=True).lines[0]


def test_not_set_up_extension_gives_guidance(monkeypatch):
    tools, chrome = _tools()
    chrome.connected = False
    with pytest.raises(ToolError, match="Set up extension"):
        _run(tools, "list_pages")


@pytest.mark.parametrize(("combo", "modifiers", "key"), [
    ("Enter", [], "Enter"),
    ("Control+A", ["Control"], "a"),
    ("Control++", ["Control"], "+"),
    ("ctrl+shift+r", ["Control", "Shift"], "r"),
])
def test_key_combos(combo, modifiers, key):
    parsed_modifiers, definition = parse_key_combo(combo)
    assert parsed_modifiers == modifiers and definition.key == key


def test_key_events_send_text_only_without_shortcut_modifiers():
    enter = key_events("Enter")
    assert enter[0]["type"] == "keyDown" and enter[0]["text"] == "\r"
    select_all = key_events("Control+A")
    assert [event["type"] for event in select_all] == ["rawKeyDown", "rawKeyDown", "keyUp", "keyUp"]
    assert "text" not in select_all[1] and select_all[1]["modifiers"] == 2
    with pytest.raises(ToolError):
        key_events("Hyper+X")


def test_stdio_server_lists_the_tools_and_explains_missing_setup(tmp_path):
    """Start the real server process and talk MCP to it, as the backend does."""
    import os
    import socket
    import sys
    from pathlib import Path

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    backend = Path(__file__).resolve().parents[3]
    env = {**os.environ, "MONAW_HOME": str(tmp_path / "home"), "AGENT_RUNTIME_DIR": str(tmp_path / "home" / "runtime"),
           "MONAW_BROWSER_EXTENSION_PORT": str(port)}

    async def scenario():
        params = StdioServerParameters(command=sys.executable, args=["-m", "app.skills.browser_use.extension"],
                                       env=env, cwd=str(backend))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                result = await session.call_tool("list_pages", {})
        return {tool.name for tool in listed.tools}, result

    names, result = asyncio.run(scenario())
    assert names == set(TOOL_NAMES)
    assert result.isError and "Set up extension" in result.content[0].text


def test_click_on_a_hidden_control_clicks_its_visible_label():
    """Custom radios (such as Apple's colour swatches) hide the input and draw its label."""
    tools, chrome = _tools()
    _run(tools, "take_snapshot")
    chrome.hidden_nodes = {13}
    chrome.label_point = {"x": 300, "y": 150}
    assert _run(tools, "click", uid="1_5").lines == ["Successfully clicked on the element"]
    pressed = [args for method, args in chrome.calls if method == "Input.dispatchMouseEvent" and args["type"] == "mousePressed"]
    assert (pressed[-1]["x"], pressed[-1]["y"]) == (300, 150)


def test_click_on_a_hidden_control_without_a_label_clicks_it_directly():
    tools, chrome = _tools()
    _run(tools, "take_snapshot")
    chrome.hidden_nodes = {13}
    output = _run(tools, "click", uid="1_5")
    assert output.lines == ["Successfully clicked on the element (it is hidden on screen, so it was clicked directly)"]
    script = [args for method, args in chrome.calls if method == "Runtime.callFunctionOn" and "el.click()" in args["functionDeclaration"]]
    assert script and script[-1]["userGesture"] is True and script[-1]["arguments"] == [{"value": False}]
    assert not any(method == "Input.dispatchMouseEvent" and args["type"] == "mousePressed" for method, args in chrome.calls)
    with __import__("pytest").raises(ToolError, match="not visible"):
        _run(tools, "hover", uid="1_5")


def test_settle_stops_when_an_in_page_navigation_finishes():
    from app.skills.browser_use.extension.session import TabSession

    async def scenario():
        session = TabSession(bridge=None, tab_id=1, main_frame_id="F1")
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, session.on_event, "Page.frameStartedLoading", {"frameId": "F1"})
        loop.call_later(0.3, session.on_event, "Page.frameStoppedLoading", {"frameId": "F1"})
        started = loop.time()
        await session.settle_after_action(timeout=5)
        return loop.time() - started

    assert asyncio.run(scenario()) < 1.0
