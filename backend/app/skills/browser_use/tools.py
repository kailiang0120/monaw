"""``browser_fetch``: read-only page retrieval over HTTP, parsed with Scrapling.

This never opens a browser. Interactive browsing and JavaScript-rendered pages
go through the built-in browser tools (``mcp__chrome-devtools__*``), served by the
Monaw extension.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import urllib.error
import urllib.request
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

from app.agent.run_context import current_interactive

from .url_policy import (
    allowed_domains as _allowed_domains,
    assert_fetch_host_public as _assert_fetch_host_public,
    fetch_domain_allowed as _fetch_domain_allowed,
    normalize_fetch_url as _normalize_fetch_url,
    validate_fetch_final_url as _validate_fetch_final_url,
)

_RENDER_MODES = {"dynamic", "stealth", "render", "browser"}
BROWSER_RENDER_HINT = (
    "This page needs JavaScript rendering, which browser_fetch does not do. Open it in the user's Chrome with "
    "mcp__chrome-devtools__new_page (or navigate_page on an existing tab), then read it with "
    "mcp__chrome-devtools__take_snapshot."
)


def _json_output(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


class _FetchTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_stack: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        if normalized in {"script", "style", "noscript", "svg"}:
            self._skip_stack.append(normalized)
        if normalized in {"p", "br", "div", "section", "article", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if self._skip_stack and self._skip_stack[-1] == normalized:
            self._skip_stack.pop()
        if normalized in {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_stack:
            return
        text = data.strip()
        if text:
            self.parts.append(text)


class _FetchLinkExtractor(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[dict[str, str]] = []
        self._active_href = ""
        self._active_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attr_map = {str(key).lower(): value or "" for key, value in attrs}
        href = attr_map.get("href", "").strip()
        if not href:
            return
        self._active_href = urljoin(self.base_url, href)
        self._active_text = []

    def handle_data(self, data: str) -> None:
        if self._active_href:
            self._active_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._active_href:
            return
        self.links.append({"url": self._active_href, "text": _compact_fetch_text(" ".join(self._active_text))[:300]})
        self._active_href = ""
        self._active_text = []


class _ScraplingParsedPage:
    def __init__(
        self,
        *,
        body: bytes,
        url: str,
        status: int = 0,
        reason: str = "",
        headers: dict[str, str] | None = None,
        encoding: str = "utf-8",
    ) -> None:
        self.body = body
        self.url = url
        self.status = status
        self.reason = reason
        self.headers = headers or {}
        self.encoding = encoding or "utf-8"
        markup = body.decode(self.encoding, errors="replace")
        self._selector = _create_scrapling_selector(markup, url=url)

    def css(self, selector: str) -> Any:
        return self._selector.css(selector)


def _compact_fetch_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _fetch_html_to_text(markup: str) -> str:
    parser = _FetchTextExtractor()
    try:
        parser.feed(markup or "")
    except Exception:
        return _compact_fetch_text(re.sub(r"<[^>]+>", " ", markup or ""))
    return _compact_fetch_text(" ".join(parser.parts))


def _truncate_fetch_content(value: str, max_chars: int) -> tuple[str, bool]:
    limit = max(500, min(int(max_chars or 20000), 200000))
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def _first_fetch_attr(obj: Any, names: tuple[str, ...], default: Any = "") -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def _call_fetch_noarg(obj: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        method = getattr(obj, name, None)
        if callable(method):
            try:
                return method()
            except Exception:
                continue
    return None


def _fetch_selection_to_text(selection: Any) -> str:
    if selection is None:
        return ""
    called = _call_fetch_noarg(selection, ("getall", "get_all", "extract"))
    if isinstance(called, list):
        return _compact_fetch_text("\n".join(str(item) for item in called))
    if called is not None:
        return _compact_fetch_text(called)
    if isinstance(selection, list):
        values: list[str] = []
        for item in selection:
            item_called = _call_fetch_noarg(item, ("get", "text", "extract"))
            values.append(str(item_called if item_called is not None else item))
        return _compact_fetch_text("\n".join(values))
    return _compact_fetch_text(selection)


def _fetch_response_html(page: Any) -> str:
    for name in ("html", "content", "text"):
        value = _first_fetch_attr(page, (name,), "")
        if isinstance(value, str) and "<" in value and ">" in value:
            return value
    body = _first_fetch_attr(page, ("body", "raw_body"), b"")
    if isinstance(body, bytes):
        encoding = str(_first_fetch_attr(page, ("encoding",), "utf-8") or "utf-8")
        return body.decode(encoding, errors="replace")
    if isinstance(body, str):
        return body
    called = _call_fetch_noarg(page, ("get", "extract"))
    if isinstance(called, str):
        return called
    return ""


def _extract_fetch_title(page: Any, markup: str) -> str:
    css = getattr(page, "css", None)
    if callable(css):
        try:
            title = _fetch_selection_to_text(css("title::text"))
            if title:
                return title[:300]
        except Exception:
            pass
    match = re.search(r"<title[^>]*>(.*?)</title>", markup or "", flags=re.I | re.S)
    return _compact_fetch_text(match.group(1))[:300] if match else ""


def _extract_fetch_links(markup: str, base_url: str, max_links: int = 200) -> list[dict[str, str]]:
    parser = _FetchLinkExtractor(base_url)
    try:
        parser.feed(markup or "")
    except Exception:
        return []
    seen: set[str] = set()
    links: list[dict[str, str]] = []
    for link in parser.links:
        url = link.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)
        links.append(link)
        if len(links) >= max_links:
            break
    return links


def _fetch_looks_blocked_or_js_empty(content: str, markup: str, status: int) -> bool:
    lowered = f"{content}\n{markup[:2000]}".lower()
    if status in {401, 403, 429, 503}:
        return True
    blockers = (
        "enable javascript",
        "checking your browser",
        "verify you are human",
        "cloudflare",
        "access denied",
        "captcha",
    )
    if any(item in lowered for item in blockers):
        return True
    return len(content.strip()) < 120 and bool(re.search(r"<script\b", markup or "", flags=re.I))


def _create_scrapling_selector(markup: str, *, url: str) -> Any:
    try:
        from scrapling import Selector
    except ImportError:
        from scrapling.parser import Selector

    try:
        return Selector(markup, url=url)
    except TypeError:
        return Selector(markup)


class _RedirectBlocked(Exception):
    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(str(error.get("error") or "Redirect blocked."))
        self.error = error


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-check every redirect hop before following it (allow-list and SSRF)."""

    def __init__(self, requested_url: str, allowed_domains: list[str]) -> None:
        super().__init__()
        self._requested_url = requested_url
        self._allowed_domains = allowed_domains

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802 - urllib API
        error = _validate_fetch_final_url(self._requested_url, newurl, self._allowed_domains)
        if error is not None:
            raise _RedirectBlocked(error)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_http_with_scrapling(
    url: str,
    *,
    timeout_ms: int,
    allowed_domains: list[str] | None = None,
) -> tuple[Any, str]:
    timeout = max(1000, min(int(timeout_ms or 30000), 120000))
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/143.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.7",
        },
    )
    opener = urllib.request.build_opener(_ValidatingRedirectHandler(url, list(allowed_domains or [])))
    try:
        with opener.open(request, timeout=timeout / 1000) as response:
            body = response.read()
            headers = {str(key).lower(): str(value) for key, value in dict(response.headers).items()}
            charset = response.headers.get_content_charset() or "utf-8"
            final_url = response.geturl() or url
            page = _ScraplingParsedPage(
                body=body,
                url=final_url,
                status=int(getattr(response, "status", 0) or 0),
                reason=str(getattr(response, "reason", "") or ""),
                headers=headers,
                encoding=charset,
            )
            return page, "http"
    except urllib.error.HTTPError as exc:
        body = exc.read()
        headers = {str(key).lower(): str(value) for key, value in dict(exc.headers).items()} if exc.headers else {}
        charset = exc.headers.get_content_charset() if exc.headers else None
        page = _ScraplingParsedPage(
            body=body,
            url=exc.geturl() or url,
            status=int(exc.code or 0),
            reason=str(exc.reason or ""),
            headers=headers,
            encoding=charset or "utf-8",
        )
        return page, "http"


def _render_fetch_response(
    page: Any,
    *,
    url: str,
    fetcher: str,
    selector: str,
    output: str,
    max_chars: int,
) -> dict[str, Any]:
    markup = _fetch_response_html(page)
    status = int(_first_fetch_attr(page, ("status", "status_code"), 0) or 0)
    final_url = str(_first_fetch_attr(page, ("url", "final_url"), url) or url)
    headers = _first_fetch_attr(page, ("headers",), {}) or {}
    reason = str(_first_fetch_attr(page, ("reason",), "") or "")
    title = _extract_fetch_title(page, markup)

    selected_text = ""
    if selector:
        css = getattr(page, "css", None)
        if callable(css):
            selected_text = _fetch_selection_to_text(css(selector))
        if not selected_text:
            selected_text = _fetch_html_to_text(markup)

    text_content = selected_text if selector else _fetch_html_to_text(markup)
    links = _extract_fetch_links(markup, final_url)

    normalized_output = output if output in {"text", "markdown", "html", "links", "metadata"} else "text"
    if normalized_output == "html":
        content = markup
    elif normalized_output in {"links", "metadata"}:
        content = ""
    else:
        content = text_content

    content, truncated = _truncate_fetch_content(content, max_chars)
    return {
        "status": "ok",
        "url": url,
        "final_url": final_url,
        "fetcher": fetcher,
        "http_status": status,
        "reason": reason,
        "title": title,
        "content": content,
        "links": links if normalized_output in {"links", "metadata"} else links[:20],
        "metadata": {
            "selector": selector,
            "output": normalized_output,
            "content_type": headers.get("content-type", "") if isinstance(headers, dict) else "",
            "content_length": len(content),
            "link_count": len(links),
        },
        "truncated": truncated,
    }


def _needs_browser_render(url: str, *, reason: str, partial: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = dict(partial or {})
    result.update(
        {
            "status": "needs_browser_render",
            "url": url,
            "reason_code": reason,
            "hint": BROWSER_RENDER_HINT,
        }
    )
    return result


def _browser_fetch_sync(
    policy: Any,
    *,
    url: str,
    mode: str,
    selector: str,
    output: str,
    max_chars: int,
    timeout_ms: int,
) -> dict[str, Any]:
    try:
        normalized_url = _normalize_fetch_url(url)
    except ValueError as exc:
        return {"status": "error", "reason_code": "invalid_url", "url": str(url or ""), "error": str(exc)}
    allowed_domains = _allowed_domains(policy)
    if not _fetch_domain_allowed(normalized_url, allowed_domains):
        return {
            "status": "error",
            "reason_code": "domain_not_allowed",
            "url": normalized_url,
            "allowed_domains": allowed_domains,
            "error": "URL host is not in Settings -> Browser allowed domains.",
        }
    if not current_interactive() and not allowed_domains:
        return {
            "status": "blocked",
            "reason_code": "non_interactive_domain_policy_required",
            "url": normalized_url,
            "error": "Non-interactive browser fetch requires an explicit allowed domain policy.",
        }

    # SSRF guard: refuse hosts that resolve to internal/non-public addresses.
    # Done after the (cheap, DNS-free) allowlist check.
    try:
        _assert_fetch_host_public(urlparse(normalized_url).hostname or "")
    except ValueError as exc:
        return {
            "status": "error",
            "reason_code": "blocked_host",
            "url": normalized_url,
            "error": str(exc),
        }

    normalized_mode = str(mode or "auto").strip().lower()
    if normalized_mode in _RENDER_MODES:
        return _needs_browser_render(normalized_url, reason="browser_render_requested")
    if normalized_mode not in {"auto", "http"}:
        normalized_mode = "auto"

    try:
        page, fetcher = _fetch_http_with_scrapling(
            normalized_url,
            timeout_ms=timeout_ms,
            allowed_domains=allowed_domains,
        )
        result = _render_fetch_response(
            page,
            url=normalized_url,
            fetcher=fetcher,
            selector=selector,
            output=output,
            max_chars=max_chars,
        )
        final_url_error = _validate_fetch_final_url(
            normalized_url,
            str(result.get("final_url") or normalized_url),
            allowed_domains,
        )
        if final_url_error is not None:
            return final_url_error
        if normalized_mode == "auto" and _fetch_looks_blocked_or_js_empty(
            result["content"], _fetch_response_html(page), result["http_status"]
        ):
            return _needs_browser_render(
                normalized_url,
                reason="http_content_empty_blocked_or_javascript_dependent",
                partial=result,
            )
        return result
    except _RedirectBlocked as exc:
        return exc.error
    except ImportError as exc:
        return {
            "status": "error",
            "reason_code": "missing_fetch_dependencies",
            "url": normalized_url,
            "error": str(exc),
            "setup": "Run `uv sync --locked` from the backend directory.",
        }
    except Exception as exc:
        return {
            "status": "error",
            "reason_code": "fetch_failed",
            "url": normalized_url,
            "fetcher": normalized_mode,
            "error": str(exc),
        }


async def browser_fetch(
    policy: Any,
    url: str,
    mode: str = "auto",
    selector: str = "",
    output: str = "text",
    max_chars: int = 20000,
    timeout_ms: int = 30000,
) -> str:
    """Fetch ``url`` over HTTP. ``policy.config["allowed_domains"]`` limits the hosts."""
    result = await asyncio.to_thread(
        _browser_fetch_sync,
        policy,
        url=url,
        mode=mode,
        selector=selector,
        output=output,
        max_chars=max_chars,
        timeout_ms=timeout_ms,
    )
    return _json_output(result)


def register_tools(registry, settings) -> None:
    from .registration import register_browser_tools

    register_browser_tools(registry, settings)
