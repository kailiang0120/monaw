import asyncio
import builtins
import json
import shutil
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from app.skills.browser_use import manager as browser_manager_module
from app.skills.browser_use import tools as browser_tools_module
from app.skills.browser_use.dom_inspection import normalize_serialized_state
from app.skills.browser_use.manager import BrowserUseManager
from app.skills.browser_use.tools import (
    _choose_reusable_tab,
    _normalize_url_for_compare,
    _prepare_snapshot,
    _score_reusable_tab,
    browser_fetch,
    browser_click,
    browser_full_page_screenshot,
    browser_find,
    browser_get_element,
    browser_navigate,
    browser_snapshot,
    browser_scroll,
    browser_type,
    _verify_typed_value,
    register_tools,
)


def test_normalize_url_for_compare_handles_bare_hosts():
    assert _normalize_url_for_compare("mail.google.com") == ("mail.google.com", "/")
    assert _normalize_url_for_compare("https://mail.google.com/mail/u/0/#inbox") == (
        "mail.google.com",
        "/mail/u/0",
    )


def test_score_reusable_tab_matches_same_host_root_request():
    score = _score_reusable_tab(
        "https://mail.google.com",
        "https://mail.google.com/mail/u/0/#inbox",
    )

    assert score >= 80


def test_choose_reusable_tab_prefers_active_matching_tab():
    tabs = [
        {
            "index": "0",
            "target_id": "docs",
            "url": "https://docs.google.com/document/d/1",
            "title": "Doc",
            "active": "false",
        },
        {
            "index": "1",
            "target_id": "gmail",
            "url": "https://mail.google.com/mail/u/0/#inbox",
            "title": "Gmail",
            "active": "true",
        },
    ]

    chosen = _choose_reusable_tab("https://mail.google.com", tabs)

    assert chosen is not None
    assert chosen["target_id"] == "gmail"


def test_choose_reusable_tab_rejects_unrelated_hosts():
    tabs = [
        {
            "index": "0",
            "target_id": "search",
            "url": "https://www.google.com/search?q=gmail",
            "title": "Search",
            "active": "true",
        }
    ]

    assert _choose_reusable_tab("https://mail.google.com", tabs) is None


def test_browser_read_only_tools_are_repeat_safe_observations():
    registered = []
    register_tools(registered, SimpleNamespace(browser={}))
    by_name = {tool["name"]: tool for tool in registered}

    for name in ("browser_find", "browser_get_element", "browser_fetch", "browser_extract_text"):
        metadata = by_name[name]["metadata"]
        assert metadata["observation"] is True
        assert metadata["mutates_state"] is False
        assert metadata["risk_level"] == "low"


def test_browser_fetch_extracts_http_page(monkeypatch):
    class FakeSelection:
        def __init__(self, value):
            self.value = value

        def getall(self):
            return [self.value]

    class FakePage:
        status = 200
        url = "https://example.com/"
        headers = {"content-type": "text/html"}
        body = b"<html><head><title>Example</title></head><body><main>Hello <a href='/next'>Next</a></main></body></html>"
        encoding = "utf-8"

        def css(self, selector):
            if selector == "title::text":
                return FakeSelection("Example")
            if selector == "main":
                return FakeSelection("Hello")
            return FakeSelection("")

    calls = []
    monkeypatch.setattr(
        browser_tools_module,
        "_fetch_http_with_scrapling",
        lambda url, *, timeout_ms: calls.append((url, timeout_ms)) or (FakePage(), "http"),
    )

    manager = SimpleNamespace(config={"allowed_domains": []})

    result = json.loads(asyncio.run(browser_fetch(manager, "example.com", mode="http", selector="main")))

    assert result["status"] == "ok"
    assert result["fetcher"] == "http"
    assert result["title"] == "Example"
    assert result["content"] == "Hello"
    assert result["links"] == [{"url": "https://example.com/next", "text": "Next"}]
    assert calls[0][0] == "https://example.com"


def test_browser_fetch_blocks_disallowed_domain():
    manager = SimpleNamespace(config={"allowed_domains": ["docs.example.com"]})

    result = json.loads(asyncio.run(browser_fetch(manager, "https://evil.example.net", mode="http")))

    assert result["status"] == "error"
    assert result["reason_code"] == "domain_not_allowed"


def test_browser_fetch_blocks_loopback_host(monkeypatch):
    # Even with no allowlist configured, SSRF guard must refuse loopback targets.
    monkeypatch.setattr(
        browser_tools_module,
        "_fetch_http_with_scrapling",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch a blocked host")),
    )
    manager = SimpleNamespace(config={"allowed_domains": []})

    result = json.loads(asyncio.run(browser_fetch(manager, "http://127.0.0.1:8000/admin", mode="http")))

    assert result["status"] == "error"
    assert result["reason_code"] == "blocked_host"


def test_browser_fetch_blocks_cloud_metadata_ip(monkeypatch):
    monkeypatch.setattr(
        browser_tools_module,
        "_fetch_http_with_scrapling",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch a blocked host")),
    )
    manager = SimpleNamespace(config={"allowed_domains": []})

    result = json.loads(
        asyncio.run(browser_fetch(manager, "http://169.254.169.254/latest/meta-data/", mode="http"))
    )

    assert result["status"] == "error"
    assert result["reason_code"] == "blocked_host"




def test_browser_fetch_blocks_redirect_to_loopback_host(monkeypatch):
    class RedirectedPage:
        status = 200
        url = "http://127.0.0.1/admin"
        body = b"<html><body>internal</body></html>"
        encoding = "utf-8"
        headers = {"content-type": "text/html"}

    monkeypatch.setattr(
        browser_tools_module,
        "_fetch_http_with_scrapling",
        lambda url, *, timeout_ms: (RedirectedPage(), "http"),
    )
    manager = SimpleNamespace(config={"allowed_domains": []})

    result = json.loads(asyncio.run(browser_fetch(manager, "https://example.com", mode="http")))

    assert result["status"] == "error"
    assert result["reason_code"] == "blocked_redirect_host"
    assert result["final_url"] == "http://127.0.0.1/admin"


def test_browser_fetch_blocks_redirect_outside_allowed_domain(monkeypatch):
    class RedirectedPage:
        status = 200
        url = "https://evil.example.net/"
        body = b"<html><body>evil</body></html>"
        encoding = "utf-8"
        headers = {"content-type": "text/html"}

    monkeypatch.setattr(
        browser_tools_module,
        "_fetch_http_with_scrapling",
        lambda url, *, timeout_ms: (RedirectedPage(), "http"),
    )
    manager = SimpleNamespace(config={"allowed_domains": ["example.com"]})

    result = json.loads(asyncio.run(browser_fetch(manager, "https://example.com", mode="http")))

    assert result["status"] == "error"
    assert result["reason_code"] == "redirect_domain_not_allowed"

def test_browser_fetch_auto_falls_back_to_dynamic_for_js_empty_page(monkeypatch):
    class StaticPage:
        status = 200
        url = "https://example.com/app"
        body = b"<html><head><title>App</title><script src='app.js'></script></head><body></body></html>"
        encoding = "utf-8"

        def css(self, selector):
            return []

    calls = []
    monkeypatch.setattr(
        browser_tools_module,
        "_fetch_http_with_scrapling",
        lambda url, *, timeout_ms: calls.append(("http", url, timeout_ms)) or (StaticPage(), "http"),
    )

    async def fake_rendered(manager, **kwargs):
        calls.append(("dynamic", kwargs["url"], kwargs["timeout_ms"]))
        return {
            "status": "ok",
            "url": kwargs["url"],
            "final_url": kwargs["url"],
            "fetcher": kwargs["fetcher"],
            "http_status": 0,
            "title": "App",
            "content": "Rendered content",
            "links": [],
            "metadata": {},
            "truncated": False,
        }

    monkeypatch.setattr(browser_tools_module, "_browser_fetch_rendered", fake_rendered)

    manager = SimpleNamespace(config={"allowed_domains": []})

    result = json.loads(asyncio.run(browser_fetch(manager, "https://example.com/app", mode="auto")))

    assert result["status"] == "ok"
    assert result["fetcher"] == "dynamic"
    assert "Rendered content" in result["content"]
    assert [call[0] for call in calls] == ["http", "dynamic"]


def test_browser_find_short_label_matches_label_token_not_substring():
    class FakePage:
        async def evaluate(self, script, selector, limit):  # noqa: ARG002
            return json.dumps(
                {
                    "elements": [
                        {"ref": "b1", "tag": "a", "role": "button", "aria_label": "Support", "labels": ["Support"]},
                        {"ref": "b2", "tag": "input", "role": "textbox", "aria_label": "To", "labels": ["To"]},
                    ]
                }
            )

    class FakeManager:
        def __init__(self):
            self.page = FakePage()

        async def ensure_browser(self, mode="auto", profile_directory=""):  # noqa: ARG002
            return SimpleNamespace()

        async def get_page(self, target_id="", index=None, create_if_missing=False):  # noqa: ARG002
            return self.page

        async def page_metadata(self, page):  # noqa: ARG002
            return {"url": "https://mail.google.com"}

    result = json.loads(asyncio.run(browser_find(FakeManager(), label="To")))

    assert result["count"] == 1
    assert result["matches"][0]["ref"] == "b2"


def test_browser_navigate_honors_wait_until():
    class FakePage:
        def __init__(self):
            self.goto_calls = []
            self.wait_calls = []

        async def goto(self, url):
            self.goto_calls.append(url)

        async def wait_for_load_state(self, state, timeout):
            self.wait_calls.append((state, timeout))

    class FakeManager:
        def __init__(self):
            self.page = FakePage()

        async def ensure_browser(self, mode="auto", profile_directory=""):
            return SimpleNamespace()

        async def get_page(self, target_id="", index=None, create_if_missing=False):
            return self.page

        async def page_metadata(self, page):
            return {"url": page.goto_calls[-1] if page.goto_calls else ""}

    manager = FakeManager()

    result = json.loads(
        asyncio.run(browser_navigate(manager, "https://example.com", wait_until="load"))
    )

    assert result["status"] == "ok"
    assert manager.page.goto_calls == ["https://example.com"]
    assert manager.page.wait_calls == [("load", 15000)]


def test_browser_click_awaits_async_page_mouse_property():
    class FakeMouse:
        def __init__(self):
            self.click_calls = []

        async def click(self, x, y, button="left", click_count=1):
            self.click_calls.append((x, y, button, click_count))

    class FakePage:
        def __init__(self):
            self.mouse_obj = FakeMouse()
            self.mouse_requests = 0

        @property
        async def mouse(self):
            self.mouse_requests += 1
            return self.mouse_obj

        async def get_target_info(self):
            return {"targetId": "page"}

        async def get_url(self):
            return "https://example.com"

        async def get_title(self):
            return "Example"

    class FakeManager:
        def __init__(self):
            self.page = FakePage()

        async def ensure_browser(self, mode="auto", profile_directory=""):
            return SimpleNamespace()

        async def get_page(self, target_id="", index=None, create_if_missing=False):
            return self.page

        async def tabs(self):
            return []

        async def page_metadata(self, page):
            return {"target_id": "page", "url": await page.get_url(), "title": await page.get_title()}

    manager = FakeManager()

    result = json.loads(asyncio.run(browser_click(manager, x=11, y=22, button="left", clicks=2)))

    assert result["status"] == "ok"
    assert manager.page.mouse_requests == 1
    assert manager.page.mouse_obj.click_calls == [(11, 22, "left", 2)]


def test_browser_manager_disables_automatic_downloads_by_default(tmp_path):
    manager = BrowserUseManager(
        {
            "headless": False,
            "keep_alive": True,
            "downloads_dir": str(tmp_path / "downloads"),
            "traces_dir": str(tmp_path / "traces"),
            "allowed_domains": [],
            "managed_profile_dir": str(tmp_path / "profile"),
        }
    )

    kwargs = manager._browser_kwargs()

    assert kwargs["accept_downloads"] is False
    assert kwargs["auto_download_pdfs"] is False


def test_browser_click_file_input_requires_approval(monkeypatch):
    created = []

    monkeypatch.setattr(browser_tools_module, "current_interactive", lambda: True)
    monkeypatch.setattr(
        browser_tools_module,
        "create_ticket",
        lambda **kwargs: created.append(kwargs) or SimpleNamespace(id="ticket-file"),
    )

    class FakeElement:
        async def click(self, **_kwargs):
            raise AssertionError("file input should not be clicked before approval")

    class FakePage:
        async def get_elements_by_css_selector(self, _selector):
            return [FakeElement()]

        async def evaluate(self, _script, *_args):
            return json.dumps({"tag": "input", "type": "file", "text": "Upload"})

    class FakeManager:
        async def ensure_browser(self, mode="auto", profile_directory=""):
            return SimpleNamespace()

        async def get_page(self, **_kwargs):
            return FakePage()

        async def tabs(self):
            return []

    result = json.loads(asyncio.run(browser_click(FakeManager(), selector="#upload")))

    assert result["status"] == "pending_approval"
    assert result["reason_code"] == "browser_file_chooser_approval_required"
    assert created[0]["tool_name"] == "browser_click"
    assert created[0]["action_type"] == "browser_file_upload"


def test_browser_click_download_link_requires_approval(monkeypatch):
    created = []

    monkeypatch.setattr(browser_tools_module, "current_interactive", lambda: True)
    monkeypatch.setattr(
        browser_tools_module,
        "create_ticket",
        lambda **kwargs: created.append(kwargs) or SimpleNamespace(id="ticket-download"),
    )

    class FakeElement:
        async def click(self, **_kwargs):
            raise AssertionError("download link should not be clicked before approval")

    class FakePage:
        async def get_elements_by_css_selector(self, _selector):
            return [FakeElement()]

        async def evaluate(self, _script, *_args):
            return json.dumps(
                {
                    "tag": "a",
                    "role": "link",
                    "href": "https://example.com/report.pdf",
                    "text": "Download report",
                }
            )

    class FakeManager:
        async def ensure_browser(self, mode="auto", profile_directory=""):
            return SimpleNamespace()

        async def get_page(self, **_kwargs):
            return FakePage()

        async def tabs(self):
            return []

    result = json.loads(asyncio.run(browser_click(FakeManager(), selector="#report")))

    assert result["status"] == "pending_approval"
    assert result["reason_code"] == "browser_download_approval_required"
    assert created[0]["tool_name"] == "browser_click"
    assert created[0]["target_path"] == "https://example.com/report.pdf"


def test_browser_type_file_input_requires_approval(monkeypatch):
    created = []

    monkeypatch.setattr(browser_tools_module, "current_interactive", lambda: True)
    monkeypatch.setattr(
        browser_tools_module,
        "create_ticket",
        lambda **kwargs: created.append(kwargs) or SimpleNamespace(id="ticket-type-file"),
    )

    class FakeElement:
        async def fill(self, *_args, **_kwargs):
            raise AssertionError("file input should not be filled before approval")

    class FakePage:
        async def get_elements_by_css_selector(self, _selector):
            return [FakeElement()]

        async def evaluate(self, _script, *_args):
            return json.dumps({"tag": "input", "type": "file", "text": "Choose file"})

    class FakeManager:
        async def ensure_browser(self, mode="auto", profile_directory=""):
            return SimpleNamespace()

        async def get_page(self, **_kwargs):
            return FakePage()

    result = json.loads(asyncio.run(browser_type(FakeManager(), text="C:/secret.txt", selector="#upload")))

    assert result["status"] == "pending_approval"
    assert result["reason_code"] == "browser_file_chooser_approval_required"
    assert created[0]["tool_name"] == "browser_type"
    assert created[0]["action_type"] == "browser_file_upload"


def test_browser_downloads_clear_requires_approval(monkeypatch, tmp_path):
    created = []
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    kept = downloads / "report.csv"
    kept.write_text("data")

    monkeypatch.setattr(browser_tools_module, "current_interactive", lambda: True)
    monkeypatch.setattr(
        browser_tools_module,
        "create_ticket",
        lambda **kwargs: created.append(kwargs) or SimpleNamespace(id="ticket-clear-downloads"),
    )

    manager = SimpleNamespace(config={"downloads_dir": str(downloads)})

    result = json.loads(asyncio.run(browser_tools_module.browser_downloads(manager, action="clear")))

    assert result["status"] == "pending_approval"
    assert result["reason_code"] == "browser_downloads_clear_approval_required"
    assert kept.exists()
    assert created[0]["tool_name"] == "browser_downloads"


def test_browser_scroll_awaits_async_page_mouse_property():
    class FakeMouse:
        def __init__(self):
            self.scroll_calls = []

        async def scroll(self, x=0, y=0, delta_x=0, delta_y=800):
            self.scroll_calls.append((x, y, delta_x, delta_y))

    class FakePage:
        def __init__(self):
            self.mouse_obj = FakeMouse()

        @property
        async def mouse(self):
            return self.mouse_obj

        async def get_target_info(self):
            return {"targetId": "page"}

        async def get_url(self):
            return "https://example.com"

        async def get_title(self):
            return "Example"

    class FakeManager:
        def __init__(self):
            self.page = FakePage()

        async def ensure_browser(self, mode="auto", profile_directory=""):
            return SimpleNamespace()

        async def get_page(self, target_id="", index=None, create_if_missing=False):
            return self.page

        async def page_metadata(self, page):
            return {"target_id": "page", "url": await page.get_url(), "title": await page.get_title()}

    manager = FakeManager()

    result = json.loads(asyncio.run(browser_scroll(manager, x=7, y=8, delta_x=1, delta_y=2)))

    assert result["status"] == "ok"
    assert manager.page.mouse_obj.scroll_calls == [(7, 8, 1, 2)]


def test_browser_type_uses_fast_dom_insert_before_element_fill():
    class FakeElement:
        def __init__(self):
            self.fill_calls = []

        async def fill(self, text, clear=True):
            self.fill_calls.append((text, clear))
            raise AssertionError("browser_type should not call fill when fast DOM insert succeeds")

    class FakePage:
        def __init__(self):
            self.element = FakeElement()
            self.value = ""
            self.evaluate_calls = []

        async def get_elements_by_css_selector(self, selector):
            assert selector == "#message"
            return [self.element]

        async def evaluate(self, script, *args):
            self.evaluate_calls.append((script, args))
            if "#message" not in args:
                return json.dumps({"value": self.value})
            if len(args) == 1:
                return json.dumps({"value": self.value})
            selector, text, clear = args
            assert selector == "#message"
            self.value = text if clear else self.value + text
            return json.dumps(
                {
                    "status": "ok",
                    "method": "dom_value",
                    "selector": selector,
                    "value": self.value,
                    "chars_inserted": len(text),
                }
            )

        async def get_url(self):
            return "https://example.com"

        async def get_title(self):
            return "Example"

    class FakeManager:
        def __init__(self):
            self.page = FakePage()

        async def ensure_browser(self, mode="auto", profile_directory=""):
            return SimpleNamespace()

        async def get_page(self, target_id="", index=None, create_if_missing=False):
            return self.page

        async def page_metadata(self, page):
            return {"url": await page.get_url(), "title": await page.get_title()}

    text = "long browser text " * 200
    manager = FakeManager()

    result = json.loads(asyncio.run(browser_type(manager, text=text, selector="#message")))

    assert result["status"] == "ok"
    assert result["input_method"]["method"] == "dom_value"
    assert result["verification"]["verified"] is True
    assert manager.page.element.fill_calls == []


class _FakeRegistry:
    def __init__(self):
        self.tools = []

    def extend(self, tools):
        self.tools.extend(tools)


def test_browser_tool_schemas_accept_common_alias_arguments():
    registry = _FakeRegistry()

    register_tools(registry, object())

    tools = {tool["name"]: tool for tool in registry.tools}
    browser_tabs_props = tools["browser_tabs"]["parameters"]["properties"]
    browser_snapshot_props = tools["browser_snapshot"]["parameters"]["properties"]
    browser_click_props = tools["browser_click"]["parameters"]["properties"]
    browser_type_props = tools["browser_type"]["parameters"]["properties"]
    assert "tab_index" in browser_tabs_props
    assert "mode" in browser_tabs_props
    assert "mode" in browser_snapshot_props
    assert "tab_target_id" in browser_snapshot_props
    assert "tab_index" in browser_snapshot_props
    assert "engine" in browser_snapshot_props
    assert "include_tree" in browser_snapshot_props
    assert "wait_for_text" in browser_click_props
    assert "verify_value" in browser_type_props
    assert "browser_get_element" in tools
    assert "browser_fill_form" in tools
    assert "browser_network_summary" in tools
    assert "browser_full_page_screenshot" in tools
    assert "fallback_proxy" in tools["browser_full_page_screenshot"]["parameters"]["properties"]


def test_browser_full_page_screenshot_applies_stealth_declutter_and_saves_file(tmp_path):
    class FakeBrowser:
        def __init__(self):
            self.init_scripts = []
            self.screenshot_calls = []

        async def _cdp_add_init_script(self, script):
            self.init_scripts.append(script)
            return "stealth-1"

        async def take_screenshot(self, path=None, full_page=False, format="png", quality=None, clip=None):
            self.screenshot_calls.append(
                {
                    "path": path,
                    "full_page": full_page,
                    "format": format,
                    "quality": quality,
                    "clip": clip,
                }
            )
            return b"full-page-image"

    class FakePage:
        def __init__(self):
            self.goto_calls = []
            self.wait_calls = []
            self.evaluate_calls = []
            self.url = "about:blank"

        async def goto(self, url):
            self.goto_calls.append(url)
            self.url = url

        async def wait_for_load_state(self, state, timeout):
            self.wait_calls.append((state, timeout))

        async def evaluate(self, script, *args):
            self.evaluate_calls.append((script, args))
            if args and isinstance(args[0], dict):
                return json.dumps(
                    {
                        "removed_count": 1,
                        "clicked_count": 1,
                        "subscription_detected": False,
                        "cloudflare_detected": False,
                        "access_denied_detected": False,
                        "blocking_overlay_count": 0,
                    }
                )
            return "true"

        async def get_url(self):
            return self.url

        async def get_title(self):
            return "Example"

        async def get_target_info(self):
            return {"targetId": "page-1"}

    class FakeManager:
        def __init__(self):
            self.browser = FakeBrowser()
            self.page = FakePage()
            self.config = {"screenshots_dir": str(tmp_path)}

        async def ensure_browser(self, mode="auto", profile_directory=""):
            return self.browser

        async def get_page(self, target_id="", index=None, create_if_missing=False):
            return self.page

        async def page_metadata(self, page):
            return {"target_id": "page-1", "url": await page.get_url(), "title": await page.get_title()}

    manager = FakeManager()

    result = json.loads(
        asyncio.run(
            browser_full_page_screenshot(
                manager,
                url="https://example.com/article",
                format="png",
            )
        )
    )

    assert result["status"] == "ok"
    assert result["full_page"] is True
    assert result["stealth"]["init"]["installed"] is True
    assert result["declutter"]["removed_count"] == 1
    assert manager.page.goto_calls == ["https://example.com/article"]
    assert manager.page.wait_calls == [("domcontentloaded", 15000)]
    assert manager.browser.screenshot_calls[0]["full_page"] is True
    assert manager.browser.screenshot_calls[0]["format"] == "png"
    output_path = Path(result["path"])
    assert output_path.exists()
    assert output_path.read_bytes() == b"full-page-image"


def test_prepare_snapshot_ranks_gmail_compose_fields_inside_limit():
    snapshot = {
        "url": "https://mail.google.com/mail/u/0/#inbox?compose=new",
        "title": "Inbox",
        "elements": [
            {"ref": "nav", "tag": "a", "role": "link", "text": "Inbox", "y": 200},
            {
                "ref": "send",
                "tag": "div",
                "role": "button",
                "text": "Send",
                "dialog_label": "New Message",
                "in_dialog": True,
                "y": 900,
            },
            {
                "ref": "to",
                "tag": "input",
                "role": "textbox",
                "labels": ["To"],
                "target_hint": "recipient_field",
                "dialog_label": "New Message",
                "in_dialog": True,
                "y": 520,
            },
            {
                "ref": "subject",
                "tag": "input",
                "role": "textbox",
                "placeholder": "Subject",
                "target_hint": "subject_field",
                "dialog_label": "New Message",
                "in_dialog": True,
                "y": 570,
            },
            {
                "ref": "body",
                "tag": "div",
                "role": "textbox",
                "aria_label": "Message Body",
                "contenteditable": "true",
                "target_hint": "message_body_field",
                "dialog_label": "New Message",
                "in_dialog": True,
                "y": 620,
            },
        ],
    }

    prepared = _prepare_snapshot(snapshot, limit=3)

    assert {element["ref"] for element in prepared["elements"]} == {"to", "subject", "body"}
    assert prepared["field_candidates"]["recipient"][0]["ref"] == "to"
    assert prepared["field_candidates"]["subject"][0]["ref"] == "subject"
    assert prepared["field_candidates"]["message_body"][0]["ref"] == "body"


def test_enhanced_dom_state_normalizes_to_snapshot_shape():
    state = SimpleNamespace(
        selector_map={
            7: SimpleNamespace(
                tag_name="input",
                attributes={
                    "id": "email",
                    "name": "to",
                    "aria-label": "To recipients",
                    "placeholder": "Recipients",
                    "required": "true",
                },
                accessibility_node=SimpleNamespace(role="textbox", name="To recipients", properties={"editable": True}),
                snapshot_node=SimpleNamespace(backend_node_id=42, bounds={"x": 10, "y": 20, "width": 300, "height": 28}),
                xpath='//*[@id="email"]',
                frame_id="main-frame",
            )
        }
    )

    snapshot, cache = normalize_serialized_state(state, limit=10)

    element = snapshot["elements"][0]
    assert snapshot["inspection_metadata"]["engine"] == "enhanced_cdp"
    assert element["ref"] == "b1"
    assert element["backend_node_id"] == 42
    assert element["role"] == "textbox"
    assert element["states"]["required"] is True
    assert element["states"]["editable"] is True
    assert element["bounds"]["center_x"] == 160
    assert cache["b1"]["css_selector"] == "#email"


def test_browser_snapshot_falls_back_to_legacy_js_when_enhanced_unavailable(monkeypatch):
    class FakePage:
        async def evaluate(self, script, *args):
            return json.dumps(
                {
                    "elements": [
                        {
                            "ref": "b1",
                            "tag": "button",
                            "role": "button",
                            "text": "Submit",
                            "y": 10,
                        }
                    ]
                }
            )

        async def get_url(self):
            return "https://example.com"

        async def get_title(self):
            return "Example"

        async def get_target_info(self):
            return {"targetId": "target-1"}

    class FakeManager:
        def __init__(self):
            self.page = FakePage()
            self.config = {"dom_inspection_engine": "auto"}
            self.stored_cache = None

        async def ensure_browser(self, mode="auto", profile_directory=""):
            return SimpleNamespace()

        async def get_page(self, target_id="", index=None, create_if_missing=False):
            return self.page

        async def diagnostics(self):
            return {"current_mode": "managed"}

        async def page_metadata(self, page):
            return {"target_id": "target-1", "url": await page.get_url(), "title": await page.get_title()}

        async def tabs(self):
            return []

        async def store_dom_snapshot_cache(self, page, *, snapshot_id, refs):
            self.stored_cache = {"snapshot_id": snapshot_id, "refs": refs}

    async def fake_enhanced(*args, **kwargs):
        return {"status": "error", "reason_code": "enhanced_dom_unavailable"}

    monkeypatch.setattr(browser_tools_module, "inspect_page_with_upstream", fake_enhanced)
    manager = FakeManager()

    result = json.loads(asyncio.run(browser_snapshot(manager, limit=5)))

    assert result["status"] == "ok"
    assert result["snapshot"]["inspection_metadata"]["engine"] == "js_fallback"
    assert result["snapshot"]["inspection_metadata"]["fallback_reason"] == "enhanced_dom_unavailable"
    assert manager.stored_cache is not None


def test_browser_get_element_uses_enhanced_cache_ref():
    class FakeElement:
        pass

    class FakePage:
        def __init__(self):
            self.selectors = []

        async def evaluate(self, script, *args):
            if args and args[0] == "b1":
                return '[data-agent-ref="b1"]'
            return json.dumps({"tag": "input", "value": "current@example.com", "text": ""})

        async def get_elements_by_css_selector(self, selector):
            self.selectors.append(selector)
            return [FakeElement()]

        async def get_url(self):
            return "https://example.com"

        async def get_title(self):
            return "Example"

        async def get_target_info(self):
            return {"targetId": "target-1"}

    class FakeManager:
        def __init__(self):
            self.page = FakePage()

        async def ensure_browser(self, mode="auto", profile_directory=""):
            return SimpleNamespace()

        async def get_page(self, target_id="", index=None, create_if_missing=False):
            return self.page

        async def page_metadata(self, page):
            return {"target_id": "target-1", "url": await page.get_url(), "title": await page.get_title()}

        async def resolve_dom_snapshot_ref(self, page, ref):
            return {
                "ref": ref,
                "css_selector": "#email",
                "metadata": {"ref": ref, "backend_node_id": 42, "role": "textbox"},
            }

    manager = FakeManager()

    result = json.loads(asyncio.run(browser_get_element(manager, ref="b1")))

    assert result["status"] == "ok"
    assert result["selector"] == '[data-agent-ref="b1"]'
    assert result["cached"]["metadata"]["backend_node_id"] == 42
    assert result["current"]["value"] == "current@example.com"


def test_browser_type_verification_accepts_gmail_contenteditable_normalization():
    result = _verify_typed_value(
        expected="Kai Liang 早晨！\n\n以下系 2026年5月26日 美股总结",
        actual="Kai Liang 早晨！ 以下系 2026年5月26日 美股总结",
        input_method={"status": "ok"},
        submitted=False,
    )

    assert result["status"] == "ok"
    assert result["verified"] is True
    assert result["normalized_match"] is True


def test_browser_type_verification_accepts_submitted_committed_value():
    result = _verify_typed_value(
        expected="kailiang0120@gmail.com",
        actual="",
        input_method={"status": "ok", "method": "dom_value"},
        submitted=True,
    )

    assert result["status"] == "ok"
    assert result["verified"] is True
    assert result["reason_code"] == "submitted_value_committed"


class _BrokenBrowser:
    async def get_tabs(self):
        raise RuntimeError("stale CDP handle")


class _RecoveredBrowser:
    async def get_tabs(self):
        return [
            SimpleNamespace(
                target_id="gmail",
                url="https://mail.google.com/mail/u/0/#inbox",
                title="Gmail",
            )
        ]

    async def get_current_target_info(self):
        return {"targetId": "gmail"}


class _RecoveringBrowserUseManager(BrowserUseManager):
    def __init__(self):
        super().__init__(
            {
                "headless": False,
                "keep_alive": True,
                "downloads_dir": "",
                "traces_dir": "",
                "allowed_domains": [],
                "managed_profile_dir": "",
            }
        )
        self._browser = _BrokenBrowser()
        self.reconnect_reason = ""

    async def _existing_browser_or_start_default(self):
        return self._browser

    async def _reconnect_current_browser(self, reason: str = ""):
        self.reconnect_reason = reason
        self._browser = _RecoveredBrowser()
        return self._browser


def test_browser_manager_reconnects_stale_handle_without_relaunching():
    manager = _RecoveringBrowserUseManager()

    tabs = asyncio.run(manager.tabs())

    assert tabs[0]["target_id"] == "gmail"
    assert tabs[0]["active"] == "true"
    assert "stale CDP handle" in manager.reconnect_reason


class _IdleBrowser:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True

    async def get_tabs(self):
        return []

    async def get_current_page(self):
        return None


class _DisconnectedBrowser(_IdleBrowser):
    @property
    def is_cdp_connected(self):
        return False


class _RestartingBrowserUseManager(BrowserUseManager):
    def __init__(self):
        super().__init__(
            {
                "headless": False,
                "keep_alive": True,
                "downloads_dir": "",
                "traces_dir": "",
                "allowed_domains": [],
                "managed_profile_dir": "",
            }
        )
        self._browser = _IdleBrowser()
        self._current_mode = "system"
        self._current_cdp_url = "http://127.0.0.1:9222"
        self.started_modes: list[str] = []

    async def _start_system_locked(self, profile_directory: str = ""):
        self.started_modes.append("system")
        self._browser = _RecoveredBrowser()
        self._current_mode = "system"
        self._current_system_connection = "attach"
        self._current_cdp_url = "ws://127.0.0.1:9222/devtools/browser/recovered"
        return self._browser


def test_browser_manager_restarts_when_existing_session_endpoint_is_dead(monkeypatch):
    manager = _RestartingBrowserUseManager()
    previous_browser = manager._browser

    monkeypatch.setattr(browser_manager_module, "_browser_use_installed", lambda: True)
    monkeypatch.setattr(browser_manager_module, "_probe_cdp_endpoint", lambda cdp_url, timeout=1.5: "")

    browser = asyncio.run(manager.ensure_browser("system"))

    assert browser is manager._browser
    assert manager.started_modes == ["system"]
    assert previous_browser.stopped is True
    assert manager._current_cdp_url == "ws://127.0.0.1:9222/devtools/browser/recovered"


def test_browser_manager_restarts_after_settings_change(monkeypatch):
    class RestartOnUpdateManager(BrowserUseManager):
        def __init__(self):
            super().__init__(
                {
                    "headless": False,
                    "keep_alive": True,
                    "downloads_dir": "",
                    "traces_dir": "",
                    "allowed_domains": [],
                    "managed_profile_dir": "",
                }
            )
            self._browser = _IdleBrowser()
            self._current_mode = "managed"
            self.starts = 0

        async def _current_session_alive_locked(self):
            return True

        async def _start_managed_locked(self):
            self.starts += 1
            self._browser = _RecoveredBrowser()
            self._current_mode = "managed"
            return self._browser

    manager = RestartOnUpdateManager()
    previous_browser = manager._browser

    manager.update({**manager.config, "headless": True})
    monkeypatch.setattr(browser_manager_module, "_browser_use_installed", lambda: True)

    browser = asyncio.run(manager.ensure_browser("managed"))

    assert previous_browser.stopped is True
    assert browser is manager._browser
    assert manager.starts == 1
    assert manager.config["headless"] is True
    assert manager._restart_requested is False


def test_browser_manager_restarts_when_internal_cdp_socket_is_disconnected(monkeypatch):
    manager = _RestartingBrowserUseManager()
    manager._browser = _DisconnectedBrowser()
    previous_browser = manager._browser

    monkeypatch.setattr(browser_manager_module, "_browser_use_installed", lambda: True)
    monkeypatch.setattr(
        browser_manager_module,
        "_probe_cdp_endpoint",
        lambda cdp_url, timeout=1.5: "ws://127.0.0.1:9222/devtools/browser/live",
    )

    browser = asyncio.run(manager.ensure_browser("system"))

    assert browser is manager._browser
    assert manager.started_modes == ["system"]
    assert previous_browser.stopped is True


def test_browser_session_liveness_checks_tab_listing(monkeypatch):
    manager = BrowserUseManager(
        {
            "headless": False,
            "keep_alive": True,
            "downloads_dir": "",
            "traces_dir": "",
            "allowed_domains": [],
            "managed_profile_dir": "",
        }
    )
    manager._browser = _BrokenBrowser()
    manager._current_mode = "managed"
    manager._current_cdp_url = "http://127.0.0.1:1234"
    monkeypatch.setattr(
        browser_manager_module,
        "_probe_cdp_endpoint",
        lambda cdp_url, timeout=1.5: "ws://127.0.0.1:1234/devtools/browser/live",
    )

    alive = asyncio.run(manager._current_session_alive_locked())

    assert alive is False
    assert "cannot list tabs" in manager._last_error


def test_browser_manager_diagnostics_marks_dead_endpoint_inactive(monkeypatch):
    manager = BrowserUseManager(
        {
            "headless": False,
            "keep_alive": True,
            "downloads_dir": "",
            "traces_dir": "",
            "allowed_domains": [],
            "managed_profile_dir": "",
        }
    )
    manager._browser = _IdleBrowser()
    manager._current_mode = "system"
    manager._current_cdp_url = "http://127.0.0.1:9222"

    monkeypatch.setattr(browser_manager_module, "_browser_use_installed", lambda: True)
    monkeypatch.setattr(browser_manager_module, "_probe_cdp_endpoint", lambda cdp_url, timeout=1.5: "")

    diagnostics = asyncio.run(manager.diagnostics())

    assert diagnostics["session_active"] is False
    assert diagnostics["cdp_endpoint_alive"] is False


def test_browser_manager_lists_local_chrome_profiles_without_browser_use_import(monkeypatch, tmp_path):
    user_data_dir = tmp_path / "Chrome" / "User Data"
    default_profile = user_data_dir / "Default"
    work_profile = user_data_dir / "Profile 1"
    default_profile.mkdir(parents=True)
    work_profile.mkdir(parents=True)
    (default_profile / "Preferences").write_text(
        json.dumps({"profile": {"name": "Personal"}}),
        encoding="utf-8",
    )
    (work_profile / "Preferences").write_text(
        json.dumps({"profile": {"name": "Work"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(browser_manager_module, "_system_chrome_user_data_dir", lambda: str(user_data_dir))
    monkeypatch.setattr(browser_manager_module, "_browser_use_installed", lambda: True)

    manager = BrowserUseManager({})

    profiles = asyncio.run(manager.list_system_profiles())

    assert profiles == [
        {"name": "Personal", "directory": "Default"},
        {"name": "Work", "directory": "Profile 1"},
    ]


def test_browser_manager_profile_listing_tolerates_browser_use_import_failure(monkeypatch):
    monkeypatch.setattr(browser_manager_module, "_list_local_chrome_profiles", lambda: [])
    monkeypatch.setattr(browser_manager_module, "_browser_use_installed", lambda: True)
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "browser_use":
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    manager = BrowserUseManager({})

    assert asyncio.run(manager.list_system_profiles()) == []


def test_browser_manager_stop_uses_previous_mode_for_managed_cleanup(monkeypatch, tmp_path):
    cleared_dirs: list[str] = []
    monkeypatch.setattr(browser_manager_module, "_clear_chrome_singleton_locks", lambda path: cleared_dirs.append(path))

    manager = BrowserUseManager(
        {
            "headless": False,
            "keep_alive": True,
            "downloads_dir": str(tmp_path / "downloads"),
            "traces_dir": str(tmp_path / "traces"),
            "allowed_domains": [],
            "managed_profile_dir": str(tmp_path / "managed-profile"),
            "screenshots_dir": str(tmp_path / "screenshots"),
        }
    )
    manager._current_mode = "managed"

    asyncio.run(manager._stop_locked())

    assert cleared_dirs == [str(tmp_path / "managed-profile")]
    assert manager._current_mode == ""


def test_clear_chrome_session_restore_state_preserves_profile_data():
    temp_dir = Path.cwd() / ".tmp-browser-session-restore-state"
    shutil.rmtree(temp_dir, ignore_errors=True)
    try:
        profile_root = temp_dir / "managed-profile"
        default_profile = profile_root / "Default"
        sessions_dir = default_profile / "Sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "Session_123").write_text("session", encoding="utf-8")
        (default_profile / "Current Tabs").write_text("tabs", encoding="utf-8")
        (default_profile / "Last Session").write_text("last", encoding="utf-8")
        (default_profile / "Cookies").write_text("cookie-data", encoding="utf-8")
        (default_profile / "Preferences").write_text(
            json.dumps({"profile": {"exit_type": "Crashed", "exited_cleanly": False}}),
            encoding="utf-8",
        )

        removed = browser_manager_module._clear_chrome_session_restore_state(str(profile_root))

        assert any("Sessions" in item for item in removed)
        assert not sessions_dir.exists()
        assert not (default_profile / "Current Tabs").exists()
        assert not (default_profile / "Last Session").exists()
        assert (default_profile / "Cookies").read_text(encoding="utf-8") == "cookie-data"
        preferences = json.loads((default_profile / "Preferences").read_text(encoding="utf-8"))
        assert preferences["profile"]["exit_type"] == "Normal"
        assert preferences["profile"]["exited_cleanly"] is True
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_reset_browser_use_runtime_can_clear_managed_session(monkeypatch):
    temp_dir = Path.cwd() / ".tmp-browser-session-reset-runtime"
    shutil.rmtree(temp_dir, ignore_errors=True)
    temp_dir.mkdir(parents=True, exist_ok=True)

    class FakeManager:
        def __init__(self):
            self.config = {"managed_profile_dir": str(temp_dir / "managed-profile")}
            self.stopped = False

        async def stop(self):
            self.stopped = True

    try:
        manager = FakeManager()
        cleared_sessions: list[str] = []
        killed_roots: list[str] = []
        cleared_locks: list[str] = []
        monkeypatch.setattr(browser_manager_module, "_MANAGER", manager)
        monkeypatch.setattr(browser_manager_module, "_MANAGER_FINGERPRINT", "abc")
        monkeypatch.setattr(browser_manager_module, "_kill_existing_chrome_processes", lambda root="": killed_roots.append(root) or 0)
        monkeypatch.setattr(browser_manager_module, "_clear_chrome_singleton_locks", lambda path: cleared_locks.append(path))
        monkeypatch.setattr(browser_manager_module, "_clear_chrome_session_restore_state", lambda path: cleared_sessions.append(path) or [])

        asyncio.run(browser_manager_module.reset_browser_use_runtime(clear_managed_session=True))

        assert manager.stopped is True
        assert browser_manager_module._MANAGER is None
        assert browser_manager_module._MANAGER_FINGERPRINT == ""
        assert killed_roots == [str(browser_manager_module._RUNTIME_ROOT)]
        assert cleared_locks == [str(temp_dir / "managed-profile")]
        assert cleared_sessions == [str(temp_dir / "managed-profile")]
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_kill_existing_chrome_processes_filters_to_runtime_root(monkeypatch):
    class FakeProc:
        def __init__(self, name: str, cmdline: list[str]):
            self.info = {"name": name, "cmdline": cmdline}
            self.terminated = False
            self.killed = False

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

    runtime_root = str(browser_manager_module._RUNTIME_ROOT)
    runtime_proc = FakeProc(
        "chrome.exe",
        [r"C:\Program Files\Google\Chrome\Application\chrome.exe", f"--user-data-dir={runtime_root}\\managed-profile"],
    )
    user_proc = FakeProc(
        "chrome.exe",
        [r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"--user-data-dir=C:\Users\Ang Kai Liang\AppData\Local\Google\Chrome\User Data"],
    )
    edge_proc = FakeProc("msedge.exe", [r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"])

    fake_psutil = types.SimpleNamespace(
        process_iter=lambda attrs: [runtime_proc, user_proc, edge_proc],
        wait_procs=lambda procs, timeout: (list(procs), []),
        NoSuchProcess=RuntimeError,
        AccessDenied=PermissionError,
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    killed = browser_manager_module._kill_existing_chrome_processes(runtime_root)

    assert killed == 1
    assert runtime_proc.terminated is True
    assert user_proc.terminated is False
    assert edge_proc.terminated is False


def test_start_managed_locked_cleans_runtime_state_and_uses_debug_flags(monkeypatch):
    class FakeBrowser:
        def __init__(self, cdp_url: str, is_local: bool, **kwargs):
            self.cdp_url = cdp_url
            self.is_local = is_local
            self.kwargs = kwargs
            self.started = False

        async def start(self):
            self.started = True

    class FakePopen:
        def __init__(self, args, **kwargs):
            captured["args"] = list(args)
            captured["kwargs"] = dict(kwargs)
            self._returncode = None

        def poll(self):
            return self._returncode

        def terminate(self):
            self._returncode = 0

        def kill(self):
            self._returncode = -9

        def wait(self, timeout=None):
            return self._returncode

    browser_use_module = types.ModuleType("browser_use")
    browser_use_module.Browser = FakeBrowser
    skill_cli_module = types.ModuleType("browser_use.skill_cli")
    utils_module = types.ModuleType("browser_use.skill_cli.utils")
    utils_module.find_chrome_executable = lambda: r"C:\Program Files\Google\Chrome\Application\chrome.exe"

    monkeypatch.setitem(sys.modules, "browser_use", browser_use_module)
    monkeypatch.setitem(sys.modules, "browser_use.skill_cli", skill_cli_module)
    monkeypatch.setitem(sys.modules, "browser_use.skill_cli.utils", utils_module)

    captured: dict[str, object] = {}
    cleared_dirs: list[str] = []
    killed_roots: list[str] = []

    async def fake_wait_for_cdp_endpoint(cdp_url: str, *, proc=None, timeout: float = 40.0) -> str:
        return "ws://127.0.0.1:4567/devtools/browser/test"

    monkeypatch.setattr(browser_manager_module, "_wait_for_cdp_endpoint", fake_wait_for_cdp_endpoint)
    monkeypatch.setattr(browser_manager_module, "_clear_chrome_singleton_locks", lambda path: cleared_dirs.append(path))
    monkeypatch.setattr(
        browser_manager_module,
        "_kill_existing_chrome_processes",
        lambda root="": killed_roots.append(root) or 1,
    )
    monkeypatch.setattr(browser_manager_module.subprocess, "Popen", FakePopen)

    temp_dir = Path.cwd() / ".tmp-browser-tools-managed"
    shutil.rmtree(temp_dir, ignore_errors=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        tmp_path = temp_dir
        manager = BrowserUseManager(
            {
                "headless": False,
                "keep_alive": True,
                "downloads_dir": str(tmp_path / "downloads"),
                "traces_dir": str(tmp_path / "traces"),
                "allowed_domains": [],
                "managed_profile_dir": str(tmp_path / "managed-profile"),
                "screenshots_dir": str(tmp_path / "screenshots"),
            }
        )

        browser = asyncio.run(manager._start_managed_locked())

        assert killed_roots == [str(browser_manager_module._RUNTIME_ROOT)]
        assert cleared_dirs == [str(tmp_path / "managed-profile")]
        assert "--remote-debugging-address=127.0.0.1" in captured["args"]
        assert "--disable-features=DevToolsDebuggingRestrictions" in captured["args"]
        assert "--disable-blink-features=AutomationControlled" in captured["args"]
        assert "--disable-infobars" in captured["args"]
        assert f"--user-data-dir={tmp_path / 'managed-profile'}" in captured["args"]
        assert captured["kwargs"]["stdout"] is not browser_manager_module.subprocess.DEVNULL
        assert captured["kwargs"]["stderr"] is not browser_manager_module.subprocess.DEVNULL
        assert isinstance(browser, FakeBrowser)
        assert browser.started is True
        assert manager._current_mode == "managed"
        assert manager._current_cdp_url == "ws://127.0.0.1:4567/devtools/browser/test"
        assert manager._recent_launches[-1]["reason_code"] == "ok"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_start_managed_locked_passes_headless_flag_to_chrome():
    args = browser_manager_module._build_managed_launch_args(
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Users\agent\profile",
        43210,
        headless=True,
    )

    assert "--headless=new" in args
    assert "--disable-gpu" in args
    assert "--disable-blink-features=AutomationControlled" in args


def test_wait_for_cdp_endpoint_bails_when_process_exits(monkeypatch):
    class ExitedProc:
        def poll(self):
            return 12

    probe_calls: list[str] = []
    monkeypatch.setattr(
        browser_manager_module,
        "_probe_cdp_endpoint",
        lambda cdp_url, timeout=1.5: probe_calls.append(cdp_url) or "",
    )

    resolved = asyncio.run(
        browser_manager_module._wait_for_cdp_endpoint(
            "http://127.0.0.1:4567",
            proc=ExitedProc(),
            timeout=1.0,
        )
    )

    assert resolved == ""
    assert probe_calls == []



def test_browser_navigate_blocks_non_interactive_without_allowed_domains(monkeypatch):
    monkeypatch.setattr(browser_tools_module, "current_interactive", lambda: False)

    class FakeManager:
        config = {"allowed_domains": []}

        async def get_page(self, *args, **kwargs):
            raise AssertionError("navigation should be blocked before opening a page")

    result = json.loads(asyncio.run(browser_navigate(FakeManager(), "https://example.com")))

    assert result["status"] == "blocked"
    assert result["reason_code"] == "non_interactive_domain_policy_required"


def test_browser_open_rejects_external_protocol_before_session_start(monkeypatch):
    monkeypatch.setattr(browser_tools_module, "current_interactive", lambda: True)

    class FakeManager:
        config = {"allowed_domains": []}

        async def ensure_browser(self, *args, **kwargs):
            raise AssertionError("invalid URL should be rejected before browser startup")

    result = json.loads(asyncio.run(browser_tools_module.browser_open(FakeManager(), "file:///C:/Windows/win.ini")))

    assert result["status"] == "error"
    assert result["reason_code"] == "invalid_url"
