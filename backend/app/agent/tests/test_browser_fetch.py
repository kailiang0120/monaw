import asyncio
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from app.skills.browser_use import tools as browser_tools_module
from app.skills.browser_use.tools import BROWSER_RENDER_HINT, browser_fetch, register_tools


@pytest.fixture(autouse=True)
def _offline_public_dns(monkeypatch):
    """Resolve the example hosts to a public address without network access."""
    from app.skills.browser_use import url_policy

    real_getaddrinfo = url_policy.socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        if str(host).endswith(("example.com", "example.net")):
            return [(2, 1, 6, "", ("93.184.215.14", 0))]
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(url_policy.socket, "getaddrinfo", getaddrinfo)


def _policy(*domains: str) -> SimpleNamespace:
    return SimpleNamespace(config={"allowed_domains": list(domains)})


def _fake_fetch(page, calls=None):
    def fetch(url, **kwargs):
        if calls is not None:
            calls.append((url, kwargs))
        return page, "http"

    return fetch


def test_browser_use_skill_registers_only_browser_fetch():
    registered = []
    register_tools(registered, SimpleNamespace(browser=SimpleNamespace(allowed_domains=["example.com"])))

    assert [tool["name"] for tool in registered] == ["browser_fetch"]
    tool = registered[0]
    assert tool["metadata"]["observation"] is True
    assert tool["metadata"]["mutates_state"] is False
    assert tool["metadata"]["risk_level"] == "low"
    assert tool["metadata"]["parallel_safe"] is True
    assert "affinity_group" not in tool
    assert tool["parameters"]["properties"]["mode"]["enum"] == ["auto", "http"]
    assert "wait_selector" not in tool["parameters"]["properties"]


def test_registered_fetch_uses_configured_allowed_domains_and_ignores_removed_arguments():
    registered = []
    register_tools(registered, SimpleNamespace(browser={"allowed_domains": ["docs.example.com"]}))

    result = json.loads(
        asyncio.run(registered[0]["callable"](url="https://evil.example.net", wait_selector="#x", network_idle=True))
    )

    assert result["reason_code"] == "domain_not_allowed"
    assert result["allowed_domains"] == ["docs.example.com"]


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
    monkeypatch.setattr(browser_tools_module, "_fetch_http_with_scrapling", _fake_fetch(FakePage(), calls))

    result = json.loads(asyncio.run(browser_fetch(_policy(), "example.com", mode="http", selector="main")))

    assert result["status"] == "ok"
    assert result["fetcher"] == "http"
    assert result["title"] == "Example"
    assert result["content"] == "Hello"
    assert result["links"] == [{"url": "https://example.com/next", "text": "Next"}]
    assert calls[0][0] == "https://example.com"
    assert calls[0][1]["allowed_domains"] == []


def test_browser_fetch_parses_real_markup_with_scrapling(monkeypatch):
    page = browser_tools_module._ScraplingParsedPage(
        body=b"<html><head><title>Docs</title></head><body><nav>Menu</nav><article><p>Body text</p></article></body></html>",
        url="https://example.com/docs",
        status=200,
        headers={"content-type": "text/html"},
    )
    monkeypatch.setattr(browser_tools_module, "_fetch_http_with_scrapling", _fake_fetch(page))

    result = json.loads(asyncio.run(browser_fetch(_policy(), "https://example.com/docs", mode="http", selector="article")))

    assert result["status"] == "ok"
    assert result["title"] == "Docs"
    assert "Body text" in result["content"]
    assert "Menu" not in result["content"]


def test_browser_fetch_rejects_non_http_url():
    result = json.loads(asyncio.run(browser_fetch(_policy(), "file:///C:/Windows/win.ini")))

    assert result["status"] == "error"
    assert result["reason_code"] == "invalid_url"


def test_browser_fetch_blocks_disallowed_domain():
    result = json.loads(asyncio.run(browser_fetch(_policy("docs.example.com"), "https://evil.example.net", mode="http")))

    assert result["status"] == "error"
    assert result["reason_code"] == "domain_not_allowed"


def test_browser_fetch_requires_domain_policy_when_not_interactive(monkeypatch):
    monkeypatch.setattr(browser_tools_module, "current_interactive", lambda: False)

    result = json.loads(asyncio.run(browser_fetch(_policy(), "https://example.com")))

    assert result["status"] == "blocked"
    assert result["reason_code"] == "non_interactive_domain_policy_required"


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/admin", "http://169.254.169.254/latest/meta-data/"])
def test_browser_fetch_blocks_non_public_hosts(monkeypatch, url):
    # Even with no allowlist configured, the SSRF guard refuses internal targets.
    monkeypatch.setattr(
        browser_tools_module,
        "_fetch_http_with_scrapling",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch a blocked host")),
    )

    result = json.loads(asyncio.run(browser_fetch(_policy(), url, mode="http")))

    assert result["status"] == "error"
    assert result["reason_code"] == "blocked_host"


def test_browser_fetch_blocks_final_url_on_loopback_host(monkeypatch):
    class RedirectedPage:
        status = 200
        url = "http://127.0.0.1/admin"
        body = b"<html><body>internal</body></html>"
        encoding = "utf-8"
        headers = {"content-type": "text/html"}

    monkeypatch.setattr(browser_tools_module, "_fetch_http_with_scrapling", _fake_fetch(RedirectedPage()))

    result = json.loads(asyncio.run(browser_fetch(_policy(), "https://example.com", mode="http")))

    assert result["status"] == "error"
    assert result["reason_code"] == "blocked_redirect_host"
    assert result["final_url"] == "http://127.0.0.1/admin"


def test_browser_fetch_blocks_final_url_outside_allowed_domain(monkeypatch):
    class RedirectedPage:
        status = 200
        url = "https://evil.example.net/"
        body = b"<html><body>evil</body></html>"
        encoding = "utf-8"
        headers = {"content-type": "text/html"}

    monkeypatch.setattr(browser_tools_module, "_fetch_http_with_scrapling", _fake_fetch(RedirectedPage()))

    result = json.loads(asyncio.run(browser_fetch(_policy("example.com"), "https://example.com", mode="http")))

    assert result["status"] == "error"
    assert result["reason_code"] == "redirect_domain_not_allowed"


def test_redirect_handler_checks_each_hop_before_following():
    handler = browser_tools_module._ValidatingRedirectHandler("https://example.com", ["example.com"])
    request = urllib.request.Request("https://example.com")

    with pytest.raises(browser_tools_module._RedirectBlocked) as outside:
        handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example.net/")
    assert outside.value.error["reason_code"] == "redirect_domain_not_allowed"

    open_handler = browser_tools_module._ValidatingRedirectHandler("https://example.com", [])
    with pytest.raises(browser_tools_module._RedirectBlocked) as internal:
        open_handler.redirect_request(request, None, 302, "Found", {}, "http://127.0.0.1/admin")
    assert internal.value.error["reason_code"] == "blocked_redirect_host"


def test_browser_fetch_never_requests_a_blocked_redirect_target(monkeypatch):
    requested: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            requested.append(self.path)
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/secret")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"secret")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Let the first (test-server) hop through; the redirect hop keeps the real check.
    monkeypatch.setattr(browser_tools_module, "_assert_fetch_host_public", lambda host: None)
    try:
        result = json.loads(
            asyncio.run(browser_fetch(_policy(), f"http://127.0.0.1:{server.server_port}/start", mode="http"))
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result["reason_code"] == "blocked_redirect_host"
    assert requested == ["/start"]


def test_browser_fetch_flags_javascript_pages_for_devtools_rendering(monkeypatch):
    class StaticPage:
        status = 200
        url = "https://example.com/app"
        body = b"<html><head><title>App</title><script src='app.js'></script></head><body></body></html>"
        encoding = "utf-8"

        def css(self, selector):
            return []

    calls = []
    monkeypatch.setattr(browser_tools_module, "_fetch_http_with_scrapling", _fake_fetch(StaticPage(), calls))

    result = json.loads(asyncio.run(browser_fetch(_policy(), "https://example.com/app", mode="auto")))

    assert len(calls) == 1
    assert result["status"] == "needs_browser_render"
    assert result["reason_code"] == "http_content_empty_blocked_or_javascript_dependent"
    assert result["hint"] == BROWSER_RENDER_HINT
    assert "mcp__chrome-devtools__new_page" in result["hint"]
    assert "take_snapshot" in result["hint"]
    assert result["title"] == "App"


def test_browser_fetch_http_mode_returns_raw_result_without_render_hint(monkeypatch):
    class StaticPage:
        status = 200
        url = "https://example.com/app"
        body = b"<html><head><script src='app.js'></script></head><body></body></html>"
        encoding = "utf-8"

    monkeypatch.setattr(browser_tools_module, "_fetch_http_with_scrapling", _fake_fetch(StaticPage()))

    result = json.loads(asyncio.run(browser_fetch(_policy(), "https://example.com/app", mode="http")))

    assert result["status"] == "ok"
    assert "hint" not in result


@pytest.mark.parametrize("mode", ["dynamic", "stealth"])
def test_old_render_modes_point_to_devtools_without_fetching(monkeypatch, mode):
    monkeypatch.setattr(
        browser_tools_module,
        "_fetch_http_with_scrapling",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("render modes must not fetch")),
    )

    result = json.loads(asyncio.run(browser_fetch(_policy(), "https://example.com", mode=mode)))

    assert result["status"] == "needs_browser_render"
    assert result["reason_code"] == "browser_render_requested"
    assert result["hint"] == BROWSER_RENDER_HINT
