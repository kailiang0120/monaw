from __future__ import annotations

import asyncio
import base64
import html
import inspect
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

from app.agent.run_context import current_interactive
from app.agent.response_attachments import (
    MAX_GENERATED_SCREENSHOT_DIMENSION_PX,
    MAX_GENERATED_SCREENSHOT_PIXELS,
    MIN_SCREENSHOT_PREVIEW_DIMENSION_PX,
    get_image_dimensions,
)

from .manager import _clear_chrome_session_restore_state, configure_browser_use_manager

from .dom_scripts import (
    _ELEMENT_METADATA_SCRIPT,
    _GLOBAL_DECLUTTER_SCRIPT,
    _INTERACTIVE_SELECTOR,
    _MATCH_REF_SCRIPT,
    _SNAPSHOT_SCRIPT,
    _STEALTH_EVALUATE_SCRIPT,
    _STEALTH_INIT_SCRIPT,
)
from .dom_inspection import inspect_page_with_upstream


def _json_output(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _decode_json_string(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return raw


_FAST_TEXT_INSERT_SCRIPT = r"""
(selector, text, clear) => {
  const element = document.querySelector(selector);
  if (!element) {
    return JSON.stringify({ status: "error", reason_code: "element_not_found", selector });
  }
  if (element.disabled || element.getAttribute("aria-disabled") === "true") {
    return JSON.stringify({ status: "error", reason_code: "element_disabled", selector });
  }

  const value = String(text ?? "");
  const shouldClear = clear !== false;
  const tag = (element.tagName || "").toLowerCase();
  const role = (element.getAttribute("role") || "").toLowerCase();
  const isContentEditable = element.isContentEditable || element.getAttribute("contenteditable") === "true";

  const dispatchTextEvents = (target, inputType) => {
    try {
      target.dispatchEvent(new InputEvent("input", {
        bubbles: true,
        cancelable: true,
        inputType,
        data: value,
      }));
    } catch (_err) {
      target.dispatchEvent(new Event("input", { bubbles: true, cancelable: true }));
    }
    target.dispatchEvent(new Event("change", { bubbles: true }));
  };

  const setNativeValue = (target, nextValue) => {
    const prototype = target instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const descriptor = Object.getOwnPropertyDescriptor(prototype, "value");
    if (descriptor && typeof descriptor.set === "function") {
      descriptor.set.call(target, nextValue);
    } else {
      target.value = nextValue;
    }
  };

  element.focus({ preventScroll: false });

  if (tag === "input" || tag === "textarea") {
    const nextValue = shouldClear ? value : String(element.value || "") + value;
    setNativeValue(element, nextValue);
    dispatchTextEvents(element, shouldClear ? "insertReplacementText" : "insertText");
    return JSON.stringify({
      status: "ok",
      method: "dom_value",
      selector,
      tag,
      value: element.value,
      chars_inserted: value.length,
    });
  }

  if (isContentEditable || role === "textbox") {
    let method = "dom_contenteditable";
    if (shouldClear) {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(element);
      selection.removeAllRanges();
      selection.addRange(range);
    }
    try {
      if (!document.execCommand("insertText", false, value)) {
        throw new Error("execCommand returned false");
      }
      method = "dom_insert_text";
    } catch (_err) {
      element.textContent = shouldClear ? value : `${element.textContent || ""}${value}`;
    }
    dispatchTextEvents(element, shouldClear ? "insertReplacementText" : "insertText");
    return JSON.stringify({
      status: "ok",
      method,
      selector,
      tag,
      text: element.innerText || element.textContent || "",
      chars_inserted: value.length,
    });
  }

  return JSON.stringify({ status: "error", reason_code: "element_not_editable", selector, tag, role });
}
"""


_CACHE_REF_SELECTOR_SCRIPT = r"""
(ref, cssSelector, xpath) => {
  const mark = (element) => {
    if (!element) return "";
    element.setAttribute("data-agent-ref", ref);
    return `[data-agent-ref="${String(ref).replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"]`;
  };
  if (cssSelector) {
    try {
      const element = document.querySelector(cssSelector);
      if (element) return mark(element);
    } catch (_err) {}
  }
  if (xpath) {
    try {
      const result = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
      if (result.singleNodeValue) return mark(result.singleNodeValue);
    } catch (_err) {}
  }
  return "";
}
"""


def _element_blob(element: dict[str, Any]) -> str:
    values: list[str] = []
    for key in (
        "target_hint",
        "aria_label",
        "placeholder",
        "name",
        "id",
        "title",
        "text",
        "value",
        "role",
        "type",
        "tag",
        "contenteditable",
        "dialog_label",
        "ax_name",
        "ax_description",
    ):
        value = element.get(key)
        if isinstance(value, list):
            values.extend(str(item) for item in value)
        elif value is not None:
            values.append(str(value))
    labels = element.get("labels")
    if isinstance(labels, list):
        values.extend(str(item) for item in labels)
    states = element.get("states")
    if isinstance(states, dict):
        values.extend(key for key, value in states.items() if value)
    control = element.get("control")
    if isinstance(control, dict):
        values.extend(str(value) for value in control.values() if value)
    return " ".join(values).lower()


def _normalize_match_text(value: Any) -> str:
    return " ".join(str(value or "").lower().replace("_", " ").replace("-", " ").split())


def _label_blob_values(element: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("aria_label", "placeholder", "name", "id", "title", "text", "ax_name", "dialog_label"):
        value = element.get(key)
        if isinstance(value, list):
            values.extend(str(item) for item in value if str(item).strip())
        elif value is not None and str(value).strip():
            values.append(str(value))
    labels = element.get("labels")
    if isinstance(labels, list):
        values.extend(str(item) for item in labels if str(item).strip())
    return values


def _matches_label_query(element: dict[str, Any], query: str) -> bool:
    normalized_query = _normalize_match_text(query)
    if not normalized_query:
        return True
    for value in _label_blob_values(element):
        normalized_value = _normalize_match_text(value)
        if not normalized_value:
            continue
        if normalized_value == normalized_query:
            return True
        tokens = normalized_value.split()
        if len(normalized_query) <= 3:
            if normalized_query in tokens:
                return True
        elif normalized_query in normalized_value:
            return True
    return False


def _field_candidate_kind(element: dict[str, Any]) -> str:
    blob = _element_blob(element)
    hint = str(element.get("target_hint") or "").lower()
    role = str(element.get("role") or "").lower()
    tag = str(element.get("tag") or "").lower()
    is_editable = (
        tag in {"input", "textarea"}
        or role == "textbox"
        or str(element.get("contenteditable") or "").lower() in {"true", "plaintext-only"}
        or bool((element.get("states") or {}).get("editable") if isinstance(element.get("states"), dict) else False)
    )
    is_action = tag in {"button", "a"} or role in {"button", "link"}

    words = set(blob.replace(":", " ").replace("/", " ").split())

    if hint == "copy_recipient_field" or bool(words & {"bcc", "cc"}):
        return "copy_recipient"
    if hint == "recipient_field" or (is_editable and bool(words & {"to", "recipient", "recipients"})):
        return "recipient"
    if hint == "subject_field" or (is_editable and "subject" in blob):
        return "subject"
    if hint == "message_body_field" or (
        is_editable
        and any(token in blob for token in ("message body", "email body", "compose body", "body", "message"))
    ):
        return "message_body"
    if hint == "search_field" or (is_editable and "search" in blob):
        return "search"
    if is_action and any(token in blob for token in ("send", "submit", "save", "continue", "next")):
        return "submit"
    return ""


def _rank_snapshot_element(element: dict[str, Any], original_index: int) -> tuple[int, int, int]:
    kind = _field_candidate_kind(element)
    score_by_kind = {
        "recipient": 950,
        "subject": 930,
        "message_body": 920,
        "copy_recipient": 860,
        "search": 780,
        "submit": 700,
    }
    score = score_by_kind.get(kind, 0)
    tag = str(element.get("tag") or "").lower()
    role = str(element.get("role") or "").lower()
    if element.get("active"):
        score += 120
    if element.get("in_dialog"):
        score += 90
    if tag in {"input", "textarea", "select"} or role in {"textbox", "combobox"}:
        score += 70
    if str(element.get("contenteditable") or "").lower() in {"true", "plaintext-only"}:
        score += 70
    if tag in {"button", "a"} or role in {"button", "link"}:
        score += 25
    if str(element.get("disabled")).lower() == "true":
        score -= 200
    states = element.get("states")
    if isinstance(states, dict):
        if states.get("disabled"):
            score -= 200
        if states.get("required"):
            score += 15
        if states.get("invalid"):
            score += 10
        if states.get("editable"):
            score += 60
    context = element.get("context")
    if isinstance(context, dict) and context.get("obscured_or_filtered"):
        score -= 120
    y = int(element.get("y") or 0)
    return (-score, y, original_index)


def _compact_field_candidate(element: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "ref": element.get("ref", ""),
        "target_hint": element.get("target_hint", ""),
        "tag": element.get("tag", ""),
        "role": element.get("role", ""),
        "type": element.get("type", ""),
        "name": element.get("name", ""),
        "aria_label": element.get("aria_label", ""),
        "labels": element.get("labels", [])[:4] if isinstance(element.get("labels"), list) else [],
        "placeholder": element.get("placeholder", ""),
        "text": str(element.get("text") or "")[:120],
        "value": str(element.get("value") or "")[:120],
        "x": element.get("x", 0),
        "y": element.get("y", 0),
        "width": element.get("width", 0),
        "height": element.get("height", 0),
    }
    for key in ("ax_name", "states", "control", "context"):
        value = element.get(key)
        if value not in (None, "", {}, []):
            compact[key] = value
    return compact


def _prepare_snapshot(snapshot: Any, limit: int) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {"elements": [], "field_candidates": {}}

    raw_elements = snapshot.get("elements")
    elements = raw_elements if isinstance(raw_elements, list) else []
    ranked = [
        item
        for _, item in sorted(
            [
                (_rank_snapshot_element(item, index), item)
                for index, item in enumerate(elements)
                if isinstance(item, dict)
            ],
            key=lambda pair: pair[0],
        )
    ]
    snapshot["elements"] = ranked[: max(1, min(limit, 200))]

    candidates: dict[str, list[dict[str, Any]]] = {
        "recipient": [],
        "copy_recipient": [],
        "subject": [],
        "message_body": [],
        "search": [],
        "submit": [],
    }
    for element in ranked:
        kind = _field_candidate_kind(element)
        if kind and len(candidates[kind]) < 5:
            candidates[kind].append(_compact_field_candidate(element))
    snapshot["field_candidates"] = {
        kind: items for kind, items in candidates.items() if items
    }
    return snapshot


def _selector_for_ref(ref: str) -> str:
    safe_ref = ref.replace('"', '\\"').strip()
    return f'[data-agent-ref="{safe_ref}"]'


def _normalize_url_for_compare(url: str) -> tuple[str, str]:
    raw = str(url or "").strip()
    parsed = urlparse(raw)
    if not parsed.scheme and raw:
        parsed = urlparse(f"https://{raw}")
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "/").rstrip("/") or "/"
    return host, path


def _score_reusable_tab(requested_url: str, tab_url: str) -> int:
    requested_host, requested_path = _normalize_url_for_compare(requested_url)
    tab_host, tab_path = _normalize_url_for_compare(tab_url)
    if not requested_host or not tab_host or requested_host != tab_host:
        return 0
    if requested_path == tab_path:
        return 100
    if requested_path == "/" or tab_path.startswith(f"{requested_path}/"):
        return 85
    return 0


def _choose_reusable_tab(
    requested_url: str,
    tabs: list[dict[str, str]],
) -> dict[str, str] | None:
    best: tuple[int, dict[str, str]] | None = None
    for tab in tabs:
        score = _score_reusable_tab(requested_url, str(tab.get("url", "")))
        if score <= 0:
            continue
        if tab.get("active") == "true":
            score += 5
        if best is None or score > best[0]:
            best = (score, tab)
    return best[1] if best and best[0] >= 80 else None


def _screenshot_path(directory: str, prefix: str, image_format: str) -> Path:
    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    return target_dir / f"{prefix}_{stamp}.{image_format}"


def _write_screenshot(data: str, directory: str, prefix: str, image_format: str) -> str:
    output_path = _screenshot_path(directory, prefix, image_format)
    output_path.write_bytes(base64.b64decode(data))
    return str(output_path)


def _write_screenshot_bytes(data: bytes, directory: str, prefix: str, image_format: str) -> str:
    output_path = _screenshot_path(directory, prefix, image_format)
    output_path.write_bytes(data)
    return str(output_path)


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


def _ip_is_blocked_target(ip: ipaddress._BaseAddress) -> bool:
    """Reject addresses that point at the host itself or internal networks
    (loopback, link-local incl. cloud-metadata 169.254.169.254, private,
    reserved, multicast, unspecified)."""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _assert_fetch_host_public(host: str) -> None:
    """Block SSRF: refuse hosts that are, or resolve to, non-public addresses."""
    cleaned = (host or "").strip().lower().strip("[]")
    if not cleaned:
        raise ValueError("URL must include a host.")
    # IP literal: check directly without a DNS lookup.
    try:
        if _ip_is_blocked_target(ipaddress.ip_address(cleaned)):
            raise ValueError("URL host resolves to a non-public address.")
        return
    except ValueError as exc:
        if "non-public" in str(exc):
            raise
    # Hostname: resolve and reject if ANY resolved address is non-public.
    try:
        infos = socket.getaddrinfo(cleaned, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise ValueError(f"Could not resolve URL host: {cleaned}") from exc
    for info in infos:
        sockaddr = info[4]
        try:
            if _ip_is_blocked_target(ipaddress.ip_address(sockaddr[0])):
                raise ValueError("URL host resolves to a non-public address.")
        except ValueError as exc:
            if "non-public" in str(exc):
                raise


def _normalize_fetch_url(raw_url: str) -> str:
    value = str(raw_url or "").strip()
    if not value:
        raise ValueError("URL is required.")
    parsed = urlparse(value)
    if not parsed.scheme:
        value = f"https://{value}"
        parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL must be an http or https URL.")
    return value


def _fetch_domain_allowed(url: str, allowed_domains: list[str]) -> bool:
    if not allowed_domains:
        return True
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    for raw_domain in allowed_domains:
        parsed = urlparse(raw_domain if "://" in raw_domain else f"https://{raw_domain}")
        allowed = (parsed.hostname or raw_domain).lower().lstrip(".")
        if host == allowed or host.endswith(f".{allowed}"):
            return True
    return False


def _validate_fetch_final_url(
    requested_url: str,
    final_url: str,
    allowed_domains: list[str],
) -> dict[str, Any] | None:
    try:
        normalized_final = _normalize_fetch_url(final_url or requested_url)
    except ValueError as exc:
        return {
            "status": "error",
            "reason_code": "invalid_final_url",
            "url": requested_url,
            "final_url": str(final_url or ""),
            "error": str(exc),
        }
    if not _fetch_domain_allowed(normalized_final, allowed_domains):
        return {
            "status": "error",
            "reason_code": "redirect_domain_not_allowed",
            "url": requested_url,
            "final_url": normalized_final,
            "allowed_domains": allowed_domains,
            "error": "Redirect target host is not in Settings -> Browser allowed domains.",
        }
    try:
        _assert_fetch_host_public(urlparse(normalized_final).hostname or "")
    except ValueError as exc:
        return {
            "status": "error",
            "reason_code": "blocked_redirect_host",
            "url": requested_url,
            "final_url": normalized_final,
            "error": str(exc),
        }
    return None


def _allowed_domains(manager: Any) -> list[str]:
    return [
        str(item).strip().lower()
        for item in (getattr(manager, "config", {}) or {}).get("allowed_domains", [])
        if str(item).strip()
    ]


def _browser_url_policy(manager: Any, raw_url: str, *, action: str) -> tuple[str, dict[str, Any] | None]:
    try:
        normalized_url = _normalize_fetch_url(raw_url)
    except ValueError as exc:
        return "", {
            "status": "error",
            "reason_code": "invalid_url",
            "url": str(raw_url or ""),
            "error": str(exc),
        }
    allowed_domains = _allowed_domains(manager)
    if not _fetch_domain_allowed(normalized_url, allowed_domains):
        return normalized_url, {
            "status": "error",
            "reason_code": "domain_not_allowed",
            "url": normalized_url,
            "allowed_domains": allowed_domains,
            "error": "URL host is not in Settings -> Browser allowed domains.",
        }
    if not current_interactive() and not allowed_domains:
        return normalized_url, {
            "status": "blocked",
            "reason_code": "non_interactive_domain_policy_required",
            "url": normalized_url,
            "action": action,
            "error": "Non-interactive browser network actions require an explicit allowed domain policy.",
        }
    if not current_interactive():
        try:
            _assert_fetch_host_public(urlparse(normalized_url).hostname or "")
        except ValueError as exc:
            return normalized_url, {
                "status": "error",
                "reason_code": "blocked_host",
                "url": normalized_url,
                "error": str(exc),
            }
    return normalized_url, None


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


def _fetch_http_with_scrapling(url: str, *, timeout_ms: int) -> tuple[Any, str]:
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
    try:
        with urllib.request.urlopen(request, timeout=timeout / 1000) as response:
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


def _browser_fetch_sync(
    manager: Any,
    *,
    url: str,
    mode: str,
    selector: str,
    output: str,
    max_chars: int,
    wait_selector: str,
    network_idle: bool,
    timeout_ms: int,
) -> dict[str, Any]:
    normalized_url = _normalize_fetch_url(url)
    allowed_domains = _allowed_domains(manager)
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

    # SSRF guard: refuse hosts that resolve to internal/non-public addresses. Done
    # after the (cheap, DNS-free) allowlist check and for every mode, so it covers
    # the browser-render path too.
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
    if normalized_mode not in {"auto", "http", "dynamic", "stealth"}:
        normalized_mode = "auto"
    if normalized_mode in {"dynamic", "stealth"}:
        return {
            "status": "needs_browser_render",
            "url": normalized_url,
            "fetcher": normalized_mode,
        }

    try:
        page, fetcher = _fetch_http_with_scrapling(normalized_url, timeout_ms=timeout_ms)
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
        if normalized_mode == "auto" and _fetch_looks_blocked_or_js_empty(result["content"], _fetch_response_html(page), result["http_status"]):
            result["status"] = "needs_browser_render"
            result["fallback_reason"] = "http_content_empty_blocked_or_javascript_dependent"
        return result
    except ImportError as exc:
        return {
            "status": "error",
            "reason_code": "missing_browser_dependencies",
            "url": normalized_url,
            "error": str(exc),
            "setup": 'pip install "scrapling>=0.4.8,<0.5.0"',
        }
    except Exception as exc:
        return {
            "status": "error",
            "reason_code": "fetch_failed",
            "url": normalized_url,
            "fetcher": normalized_mode,
            "error": str(exc),
        }


def _discard_invalid_screenshot(path: str | Path) -> dict[str, Any] | None:
    target = Path(path)
    dimensions = get_image_dimensions(target)
    if dimensions is None:
        return None
    width, height = dimensions
    too_small = width < MIN_SCREENSHOT_PREVIEW_DIMENSION_PX or height < MIN_SCREENSHOT_PREVIEW_DIMENSION_PX
    too_large = (
        width > MAX_GENERATED_SCREENSHOT_DIMENSION_PX
        or height > MAX_GENERATED_SCREENSHOT_DIMENSION_PX
        or width * height > MAX_GENERATED_SCREENSHOT_PIXELS
    )
    if not too_small and not too_large:
        return None
    try:
        target.unlink()
    except OSError:
        pass
    return {
        "reason_code": "screenshot_too_small" if too_small else "screenshot_too_large",
        "width": width,
        "height": height,
        "min_dimension": MIN_SCREENSHOT_PREVIEW_DIMENSION_PX,
        "max_dimension": MAX_GENERATED_SCREENSHOT_DIMENSION_PX,
        "max_pixels": MAX_GENERATED_SCREENSHOT_PIXELS,
    }


def _write_previewable_screenshot(
    data: str,
    directory: str,
    prefix: str,
    image_format: str,
) -> tuple[str, dict[str, Any] | None]:
    output_path = _write_screenshot(data, directory, prefix, image_format)
    skipped = _discard_invalid_screenshot(output_path)
    return ("", skipped) if skipped else (output_path, None)


def _browser_id(browser: Any) -> int:
    return id(browser)


def _invalidate_dom_cache(manager: Any, reason: str) -> None:
    clear = getattr(manager, "clear_dom_snapshot_cache", None)
    if callable(clear):
        clear(reason)


async def _store_dom_cache(manager: Any, page: Any, *, snapshot_id: str, refs: dict[str, dict[str, Any]]) -> None:
    store = getattr(manager, "store_dom_snapshot_cache", None)
    if callable(store):
        await store(page, snapshot_id=snapshot_id, refs=refs)


async def _install_stealth_init(manager, browser) -> dict[str, Any]:
    add_init_script = getattr(browser, "_cdp_add_init_script", None)
    if not callable(add_init_script):
        return {"installed": False, "reason": "browser does not expose CDP init scripts"}

    installed_for = getattr(manager, "_stealth_init_browser_id", None)
    if installed_for == _browser_id(browser):
        return {"installed": True, "reused": True}

    try:
        identifier = await add_init_script(_STEALTH_INIT_SCRIPT)
    except Exception as exc:
        return {"installed": False, "error": str(exc)}

    setattr(manager, "_stealth_init_browser_id", _browser_id(browser))
    setattr(manager, "_stealth_init_identifier", str(identifier or ""))
    return {"installed": True, "identifier": str(identifier or "")}


async def _apply_stealth_runtime(page) -> dict[str, Any]:
    try:
        await page.evaluate(_STEALTH_EVALUATE_SCRIPT)
    except Exception as exc:
        return {"applied": False, "error": str(exc)}
    return {"applied": True}


async def _run_global_declutter(
    page,
    *,
    remove_fixed: bool = True,
    click_close: bool = True,
) -> dict[str, Any]:
    try:
        result = await page.evaluate(
            _GLOBAL_DECLUTTER_SCRIPT,
            {"removeFixed": bool(remove_fixed), "clickClose": bool(click_close)},
        )
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    decoded = _decode_json_string(result)
    if isinstance(decoded, dict):
        return {"status": "ok", **decoded}
    return {"status": "ok", "result": decoded}


def _fallback_proxy_url(url: str, proxy: str) -> str:
    normalized = str(url or "").strip()
    if not normalized:
        return ""
    proxy_path = quote(normalized, safe=":/")
    if proxy == "archive_is":
        return f"https://archive.is/newest/{proxy_path}"
    if proxy == "smry_ai":
        return f"https://smry.ai/{proxy_path}"
    return ""


def _fallback_suggestions(url: str) -> list[dict[str, str]]:
    normalized = str(url or "").strip()
    if not normalized:
        return []
    wayback_url = "https://web.archive.org/save/" + quote(
        normalized,
        safe=":/?#[]@!$&'()*+,;=%",
    )
    archive_url = _fallback_proxy_url(normalized, "archive_is")
    smry_url = _fallback_proxy_url(normalized, "smry_ai")
    return [
        {
            "service": "archive.is",
            "url": archive_url,
            "note": "Public archive/proxy view; use only when sending the URL to a third-party service is acceptable.",
        },
        {
            "service": "SMRY.ai",
            "url": smry_url,
            "note": "Third-party simplified article view; use only for public articles where proxy access is acceptable.",
        },
        {
            "service": "Wayback save",
            "url": wayback_url,
            "note": "Archive the page with the Internet Archive instead of bypassing access controls.",
        },
    ]


def _cleanup_needs_fallback(cleanup: dict[str, Any]) -> bool:
    return bool(
        cleanup.get("subscription_detected")
        or cleanup.get("cloudflare_detected")
        or cleanup.get("access_denied_detected")
        or int(cleanup.get("blocking_overlay_count") or 0) > 0
    )


async def _sleep_after_action(delay_seconds: float = 0.4) -> None:
    await asyncio.sleep(delay_seconds)


async def _page_mouse(page):
    mouse = getattr(page, "mouse")
    if inspect.isawaitable(mouse):
        return await mouse
    if callable(mouse):
        maybe_mouse = mouse()
        if inspect.isawaitable(maybe_mouse):
            return await maybe_mouse
        return maybe_mouse
    return mouse


async def _wait_after_navigation(page, wait_until: str, timeout_seconds: float = 15) -> None:
    normalized = str(wait_until or "domcontentloaded").strip().lower()
    if normalized not in {"domcontentloaded", "load", "networkidle"}:
        normalized = "domcontentloaded"
    wait_for_load_state = getattr(page, "wait_for_load_state", None)
    if callable(wait_for_load_state):
        await wait_for_load_state(normalized, timeout=timeout_seconds * 1000)


async def _verify_browser_condition(
    page,
    *,
    selector: str = "",
    text: str = "",
    url_contains: str = "",
    timeout_seconds: float = 10,
) -> dict[str, Any]:
    if not selector and not text and not url_contains:
        return {"status": "ok", "verified": False}
    deadline = asyncio.get_running_loop().time() + max(timeout_seconds, 0.2)
    while asyncio.get_running_loop().time() < deadline:
        if selector:
            exists = await page.evaluate("(selector) => !!document.querySelector(selector)", selector)
            if str(exists).lower() == "true":
                return {"status": "ok", "verified": True, "selector": selector}
        if text:
            matched_ref = await page.evaluate(
                _MATCH_REF_SCRIPT,
                _INTERACTIVE_SELECTOR,
                text,
                False,
                False,
            )
            if str(matched_ref or "").strip():
                return {"status": "ok", "verified": True, "text": text, "ref": str(matched_ref).strip()}
        if url_contains:
            current_url = await page.get_url()
            if url_contains in current_url:
                return {"status": "ok", "verified": True, "url": current_url}
        await asyncio.sleep(0.25)
    return {
        "status": "error",
        "verified": False,
        "error": "Timed out waiting for requested browser verification.",
        "reason_code": "verification_timeout",
        "selector": selector,
        "text": text,
        "url_contains": url_contains,
    }


async def _current_page(manager, *, mode: str = "auto", profile_directory: str = "", target_id: str = "", index: int | None = None, create_if_missing: bool = False):
    await manager.ensure_browser(mode, profile_directory)
    page = await manager.get_page(
        target_id=target_id,
        index=index,
        create_if_missing=create_if_missing,
    )
    if page is None:
        raise RuntimeError("No browser page is active. Open a page first with browser_open.")
    return page


async def _resolve_element(
    manager,
    page,
    *,
    ref: str = "",
    selector: str = "",
    text: str = "",
    exact_text: bool = False,
    prefer_editable: bool = False,
):
    chosen_selector = selector.strip()
    normalized_ref = ref.strip()
    stale_ref: dict[str, Any] | None = None
    if normalized_ref and manager is not None:
        resolve_cached = getattr(manager, "resolve_dom_snapshot_ref", None)
        cached_ref = await resolve_cached(page, normalized_ref) if callable(resolve_cached) else None
        if isinstance(cached_ref, dict) and cached_ref.get("stale"):
            stale_ref = cached_ref
        elif isinstance(cached_ref, dict):
            cache_selector = await page.evaluate(
                _CACHE_REF_SELECTOR_SCRIPT,
                normalized_ref,
                str(cached_ref.get("css_selector") or ""),
                str(cached_ref.get("xpath") or ""),
            )
            if str(cache_selector or "").strip():
                chosen_selector = str(cache_selector).strip()
    if normalized_ref and not chosen_selector:
        chosen_selector = _selector_for_ref(normalized_ref)

    if not chosen_selector and text.strip():
        result = await page.evaluate(
            _MATCH_REF_SCRIPT,
            _INTERACTIVE_SELECTOR,
            text,
            exact_text,
            prefer_editable,
        )
        generated_ref = str(result or "").strip()
        if generated_ref:
            chosen_selector = _selector_for_ref(generated_ref)

    if not chosen_selector:
        raise ValueError("Provide ref, selector, text, or coordinates.")

    elements = await page.get_elements_by_css_selector(chosen_selector)
    if not elements:
        if stale_ref:
            raise RuntimeError(
                f"Ref '{normalized_ref}' is stale after page change; call browser_snapshot again. "
                "needs_snapshot=true"
            )
        raise RuntimeError(f"No element matched selector '{chosen_selector}'.")
    return elements[0], chosen_selector


async def _element_metadata(page, selector: str) -> dict[str, Any] | None:
    try:
        result = await page.evaluate(_ELEMENT_METADATA_SCRIPT, selector)
    except Exception:
        return None
    decoded = _decode_json_string(result)
    return decoded if isinstance(decoded, dict) else None


async def _fast_insert_text(page, element, selector: str, text: str, clear: bool) -> dict[str, Any]:
    try:
        result = await page.evaluate(_FAST_TEXT_INSERT_SCRIPT, selector, text, clear)
        decoded = _decode_json_string(result)
        if isinstance(decoded, dict) and decoded.get("status") == "ok":
            return decoded
    except Exception as exc:
        decoded = {"status": "error", "error": str(exc), "reason_code": "dom_insert_failed"}
    else:
        decoded = decoded if isinstance(decoded, dict) else {"status": "error", "result": decoded}

    await element.fill(text, clear=clear)
    return {
        "status": "ok",
        "method": "element_fill_fallback",
        "fallback_reason": decoded.get("reason_code", "dom_insert_failed"),
        "chars_inserted": len(text),
    }


def _normalize_typed_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\u00a0", " ").split())


def _verify_typed_value(
    *,
    expected: str,
    actual: str,
    input_method: dict[str, Any],
    submitted: bool,
) -> dict[str, Any]:
    normalized_expected = _normalize_typed_text(expected)
    normalized_actual = _normalize_typed_text(actual)
    exact_match = actual == expected
    normalized_match = bool(normalized_expected) and normalized_actual == normalized_expected
    contains_match = bool(normalized_expected) and normalized_expected in normalized_actual
    input_succeeded = input_method.get("status") == "ok"
    if exact_match or normalized_match or contains_match:
        return {
            "status": "ok",
            "verified": True,
            "expected_value": expected,
            "actual_value": actual,
            "normalized_match": not exact_match,
            "reason_code": "",
        }
    if submitted and input_succeeded:
        return {
            "status": "ok",
            "verified": True,
            "expected_value": expected,
            "actual_value": actual,
            "reason_code": "submitted_value_committed",
            "note": "The field value changed after Enter/submit; treating successful insertion plus submit as committed.",
        }
    return {
        "status": "error",
        "verified": False,
        "expected_value": expected,
        "actual_value": actual,
        "normalized_expected": normalized_expected,
        "normalized_actual": normalized_actual,
        "reason_code": "value_mismatch",
    }


async def browser_session(
    manager,
    action: str = "status",
    mode: str = "auto",
    profile_directory: str = "",
) -> str:
    normalized_action = action.strip().lower()
    if normalized_action == "status":
        return _json_output({"status": "ok", **(await manager.diagnostics())})
    if normalized_action == "doctor":
        diagnostics = await manager.diagnostics()
        next_steps: list[str] = []
        recent_launches = diagnostics.get("recent_launches")
        latest_launch = recent_launches[-1] if isinstance(recent_launches, list) and recent_launches else {}
        latest_reason = latest_launch.get("reason_code") if isinstance(latest_launch, dict) else ""
        if not diagnostics.get("available"):
            next_steps.append("Install the browser-use Python package in the backend environment.")
        elif latest_reason in {"managed_cdp_timeout", "managed_chrome_exited"}:
            next_steps.append("Call browser_session(action=\"reset\") once, then retry the original browser intent.")
            next_steps.append(f"Review Chrome subprocess output at {diagnostics.get('chrome_log_path')}.")
        elif latest_reason in {"chrome_executable_missing", "system_launch_disabled"}:
            next_steps.append("Stop and report the browser launch blocker; user action is required.")
        elif diagnostics.get("session_active") and diagnostics.get("cdp_endpoint_alive") is False:
            next_steps.append("Stop/reset the browser session because the current CDP endpoint is not reachable.")
        elif diagnostics.get("last_error"):
            next_steps.append("Review last_error and use managed mode unless you have started Chrome with a non-default DevTools user-data-dir.")
        else:
            next_steps.append("Browser diagnostics look healthy.")
        return _json_output({"status": "ok", **diagnostics, "next_steps": next_steps})
    if normalized_action == "list_profiles":
        return _json_output(
            {
                "status": "ok",
                "profiles": await manager.list_system_profiles(),
            }
        )
    if normalized_action == "stop":
        await manager.stop()
        return _json_output({"status": "ok", "message": "Browser session stopped."})
    if normalized_action == "reset":
        await manager.stop()
        _clear_chrome_session_restore_state(str(manager.config.get("managed_profile_dir", "")))
        await manager.ensure_browser(mode, profile_directory)
        return _json_output({"status": "ok", **(await manager.diagnostics())})
    if normalized_action == "use_managed":
        await manager.ensure_browser("managed")
        return _json_output({"status": "ok", **(await manager.diagnostics())})
    if normalized_action == "use_system":
        await manager.ensure_browser("system", profile_directory)
        return _json_output({"status": "ok", **(await manager.diagnostics())})
    raise ValueError(f"Unsupported browser_session action '{action}'.")


async def browser_open(
    manager,
    url: str = "",
    mode: str = "auto",
    profile_directory: str = "",
    new_tab: bool = False,
    reuse_existing: bool = True,
    wait_until: str = "domcontentloaded",
) -> str:
    if url:
        url, policy_error = _browser_url_policy(manager, url, action="browser_open")
        if policy_error:
            return _json_output(policy_error)
    browser = await manager.ensure_browser(mode, profile_directory)
    page = await _current_page(
        manager,
        mode=mode,
        profile_directory=profile_directory,
        create_if_missing=True,
    )
    reused_tab: dict[str, str] | None = None
    if new_tab:
        if url:
            page = await browser.new_page(url)
            await _wait_after_navigation(page, wait_until)
        else:
            page = await browser.new_page()
    elif url and reuse_existing:
        reusable_tab = _choose_reusable_tab(url, await manager.tabs())
        if reusable_tab is not None:
            page = await manager.get_page(
                target_id=reusable_tab.get("target_id", ""),
                create_if_missing=True,
            )
            reused_tab = reusable_tab
        else:
            await page.goto(url)
            await _wait_after_navigation(page, wait_until)
    elif url:
        await page.goto(url)
        await _wait_after_navigation(page, wait_until)
    await _sleep_after_action(0.8 if url else 0.3)
    if url or new_tab or reused_tab:
        _invalidate_dom_cache(manager, "browser_open")
    return _json_output(
        {
            "status": "ok",
            "mode": (await manager.diagnostics()).get("current_mode", ""),
            "reused_existing_tab": bool(reused_tab),
            "reused_tab": reused_tab,
            "page": await manager.page_metadata(page),
            "wait_until": wait_until,
        }
    )

async def browser_navigate(
    manager,
    url: str,
    tab_target_id: str = "",
    tab_index: int = -1,
    wait_until: str = "domcontentloaded",
) -> str:
    url, policy_error = _browser_url_policy(manager, url, action="browser_navigate")
    if policy_error:
        return _json_output(policy_error)
    page = await _current_page(
        manager,
        target_id=tab_target_id,
        index=None if tab_index < 0 else tab_index,
        create_if_missing=True,
    )
    await page.goto(url)
    await _wait_after_navigation(page, wait_until)
    await _sleep_after_action(0.8)
    _invalidate_dom_cache(manager, "browser_navigate")
    return _json_output({"status": "ok", "page": await manager.page_metadata(page), "wait_until": wait_until})


async def browser_back(manager) -> str:
    page = await _current_page(manager)
    await page.go_back()
    await _sleep_after_action(0.6)
    _invalidate_dom_cache(manager, "browser_back")
    return _json_output({"status": "ok", "page": await manager.page_metadata(page)})


async def browser_forward(manager) -> str:
    page = await _current_page(manager)
    await page.go_forward()
    await _sleep_after_action(0.6)
    _invalidate_dom_cache(manager, "browser_forward")
    return _json_output({"status": "ok", "page": await manager.page_metadata(page)})


async def browser_reload(manager) -> str:
    page = await _current_page(manager)
    await page.reload()
    await _sleep_after_action(0.8)
    _invalidate_dom_cache(manager, "browser_reload")
    return _json_output({"status": "ok", "page": await manager.page_metadata(page)})


async def browser_tabs(
    manager,
    action: str = "list",
    url: str = "",
    target_id: str = "",
    index: int = -1,
    tab_index: int = -1,
    mode: str = "auto",
) -> str:
    browser = await manager.ensure_browser(mode)
    normalized_action = action.strip().lower()
    selected_index = index if index >= 0 else tab_index
    if normalized_action == "list":
        return _json_output({"status": "ok", "tabs": await manager.tabs()})
    if normalized_action == "new":
        page = await browser.new_page(url or None)
        await _sleep_after_action(0.6)
        _invalidate_dom_cache(manager, "browser_tabs_new")
        return _json_output({"status": "ok", "page": await manager.page_metadata(page), "tabs": await manager.tabs()})
    if normalized_action == "switch":
        page = await manager.switch_tab(target_id=target_id, index=None if selected_index < 0 else selected_index)
        _invalidate_dom_cache(manager, "browser_tabs_switch")
        return _json_output({"status": "ok", "page": page, "tabs": await manager.tabs()})
    if normalized_action == "close":
        page = await manager.get_page(target_id=target_id, index=None if selected_index < 0 else selected_index, create_if_missing=False)
        if page is None:
            raise RuntimeError("No active tab to close.")
        await browser.close_page(page)
        await _sleep_after_action(0.3)
        _invalidate_dom_cache(manager, "browser_tabs_close")
        return _json_output({"status": "ok", "tabs": await manager.tabs()})
    raise ValueError(f"Unsupported browser_tabs action '{action}'.")


async def browser_snapshot(
    manager,
    include_screenshot: bool = False,
    limit: int = 40,
    mode: str = "auto",
    tab_target_id: str = "",
    tab_index: int = -1,
    engine: str = "auto",
    include_tree: bool = False,
    include_scroll_info: bool = True,
    include_hidden_hints: bool = True,
) -> str:
    page = await _current_page(
        manager,
        mode=mode,
        target_id=tab_target_id,
        index=None if tab_index < 0 else tab_index,
    )
    requested_limit = max(1, min(limit, 200))
    configured_engine = str(manager.config.get("dom_inspection_engine", "auto") or "auto").lower()
    requested_engine = str(engine or "auto").lower()
    snapshot_engine = configured_engine if requested_engine == "auto" else requested_engine
    enhanced = await inspect_page_with_upstream(
        page,
        manager,
        limit=requested_limit,
        engine=snapshot_engine,
        include_tree=include_tree,
        include_scroll_info=include_scroll_info,
        include_hidden_hints=include_hidden_hints,
    )
    ref_cache: dict[str, dict[str, Any]] = {}
    if enhanced.get("status") == "ok" and isinstance(enhanced.get("snapshot"), dict):
        snapshot = _prepare_snapshot(enhanced["snapshot"], requested_limit)
        ref_cache = enhanced.get("ref_cache") if isinstance(enhanced.get("ref_cache"), dict) else {}
    else:
        collection_limit = max(requested_limit * 6, 240)
        snapshot_raw = await page.evaluate(
            _SNAPSHOT_SCRIPT,
            _INTERACTIVE_SELECTOR,
            min(collection_limit, 800),
        )
        snapshot = _prepare_snapshot(_decode_json_string(snapshot_raw), requested_limit)
        if isinstance(snapshot, dict):
            metadata = snapshot.setdefault("inspection_metadata", {})
            if isinstance(metadata, dict):
                metadata.update(
                    {
                        "engine": "js_fallback",
                        "fallback_reason": str(enhanced.get("reason_code") or enhanced.get("error") or "enhanced_dom_unavailable"),
                        "requested_engine": snapshot_engine,
                    }
                )
    if isinstance(snapshot, dict):
        metadata = snapshot.setdefault("inspection_metadata", {})
        if isinstance(metadata, dict):
            snapshot_id = f"dom-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
            metadata.setdefault("snapshot_id", snapshot_id)
            metadata.setdefault("engine", "js_fallback")
            await _store_dom_cache(
                manager,
                page,
                snapshot_id=str(metadata.get("snapshot_id") or snapshot_id),
                refs=ref_cache,
            )
    payload: dict[str, Any] = {
        "status": "ok",
        "mode": (await manager.diagnostics()).get("current_mode", ""),
        "page": await manager.page_metadata(page),
        "tabs": await manager.tabs(),
        "snapshot": snapshot,
    }
    if include_screenshot:
        image_data = await page.screenshot()
        screenshot_path, skipped_screenshot = _write_previewable_screenshot(
            image_data,
            manager.config["screenshots_dir"],
            "browser_snapshot",
            "png",
        )
        if screenshot_path:
            payload["screenshot_path"] = screenshot_path
        if skipped_screenshot:
            payload["screenshot_skipped"] = skipped_screenshot
    return _json_output(payload)


async def browser_click(
    manager,
    ref: str = "",
    selector: str = "",
    text: str = "",
    exact_text: bool = False,
    x: int | None = None,
    y: int | None = None,
    button: str = "left",
    clicks: int = 1,
    tab_target_id: str = "",
    tab_index: int = -1,
    wait_for_selector: str = "",
    wait_for_text: str = "",
    wait_for_url_contains: str = "",
    expect_new_tab: bool = False,
    timeout_seconds: float = 10,
) -> str:
    page = await _current_page(
        manager,
        target_id=tab_target_id,
        index=None if tab_index < 0 else tab_index,
    )
    before_tabs = await manager.tabs()
    clicked = {"mode": "coordinates"} if x is not None and y is not None else {}
    resolved_selector = ""
    target_after: dict[str, Any] | None = None

    if x is not None and y is not None:
        mouse = await _page_mouse(page)
        await mouse.click(int(x), int(y), button=button, click_count=max(1, clicks))
        clicked.update({"x": int(x), "y": int(y), "button": button, "clicks": max(1, clicks)})
    else:
        element, resolved_selector = await _resolve_element(
            manager,
            page,
            ref=ref,
            selector=selector,
            text=text,
            exact_text=exact_text,
        )
        element_meta = await _element_metadata(page, resolved_selector)
        await element.click(button=button, click_count=max(1, clicks))
        clicked.update(
            {
                "mode": "element",
                "selector": resolved_selector,
                "ref": ref or "",
                "text": text,
                "element": element_meta,
            }
        )

    await _sleep_after_action(0.5)
    if resolved_selector:
        target_after = await _element_metadata(page, resolved_selector)
        clicked["element_after"] = target_after
    tabs_after = await manager.tabs()
    verification = await _verify_browser_condition(
        page,
        selector=wait_for_selector,
        text=wait_for_text,
        url_contains=wait_for_url_contains,
        timeout_seconds=timeout_seconds,
    )
    if expect_new_tab and len(tabs_after) <= len(before_tabs):
        verification = {
            "status": "error",
            "verified": False,
            "error": "Expected a new tab after click, but tab count did not increase.",
            "reason_code": "expected_new_tab_missing",
        }
    _invalidate_dom_cache(manager, "browser_click")
    return _json_output(
        {
            "status": "ok" if verification.get("status") == "ok" else "error",
            "clicked": clicked,
            "target_after": target_after,
            "verification": verification,
            "page": await manager.page_metadata(page),
            "tabs_before": before_tabs,
            "tabs_after": tabs_after,
            "cache_invalidated": True,
        }
    )


async def browser_type(
    manager,
    text: str,
    ref: str = "",
    selector: str = "",
    target_text: str = "",
    exact_text: bool = False,
    submit: bool = False,
    clear: bool = True,
    tab_target_id: str = "",
    tab_index: int = -1,
    verify_value: bool = True,
    wait_for_text: str = "",
    timeout_seconds: float = 10,
) -> str:
    page = await _current_page(
        manager,
        target_id=tab_target_id,
        index=None if tab_index < 0 else tab_index,
    )
    if ref or selector:
        element, resolved_selector = await _resolve_element(
            manager,
            page,
            ref=ref,
            selector=selector,
            exact_text=exact_text,
            prefer_editable=True,
        )
    else:
        element, resolved_selector = await _resolve_element(
            manager,
            page,
            text=target_text,
            exact_text=exact_text,
            prefer_editable=True,
        )
    target_before = await _element_metadata(page, resolved_selector)
    input_method = await _fast_insert_text(page, element, resolved_selector, text, clear)
    if submit:
        await page.press("Enter")
    await _sleep_after_action(0.3)
    target_after = await _element_metadata(page, resolved_selector)
    verification: dict[str, Any] = {"status": "ok", "verified": False}
    if verify_value:
        actual_value = str((target_after or {}).get("value") or (target_after or {}).get("text") or "")
        verification = _verify_typed_value(
            expected=text,
            actual=actual_value,
            input_method=input_method,
            submitted=submit,
        )
    if wait_for_text:
        verification = await _verify_browser_condition(page, text=wait_for_text, timeout_seconds=timeout_seconds)
    _invalidate_dom_cache(manager, "browser_type")
    return _json_output(
        {
            "status": "ok" if verification.get("status") == "ok" else "error",
            "selector": resolved_selector,
            "target": target_before,
            "target_after": target_after,
            "input_method": input_method,
            "verification": verification,
            "submitted": submit,
            "page": await manager.page_metadata(page),
            "cache_invalidated": True,
        }
    )


async def browser_press(
    manager,
    key: str,
    tab_target_id: str = "",
    tab_index: int = -1,
) -> str:
    page = await _current_page(
        manager,
        target_id=tab_target_id,
        index=None if tab_index < 0 else tab_index,
    )
    await page.press(key)
    await _sleep_after_action(0.2)
    _invalidate_dom_cache(manager, "browser_press")
    return _json_output({"status": "ok", "key": key, "page": await manager.page_metadata(page), "cache_invalidated": True})


async def browser_select_option(
    manager,
    values: list[str],
    ref: str = "",
    selector: str = "",
    tab_target_id: str = "",
    tab_index: int = -1,
) -> str:
    page = await _current_page(
        manager,
        target_id=tab_target_id,
        index=None if tab_index < 0 else tab_index,
    )
    element, resolved_selector = await _resolve_element(manager, page, ref=ref, selector=selector)
    chosen_values: list[str] = [value for value in values if str(value).strip()]
    if not chosen_values:
        raise ValueError("Provide at least one option value.")
    await element.select_option(chosen_values)
    await _sleep_after_action(0.2)
    _invalidate_dom_cache(manager, "browser_select_option")
    return _json_output(
        {
            "status": "ok",
            "selector": resolved_selector,
            "values": chosen_values,
            "page": await manager.page_metadata(page),
            "cache_invalidated": True,
        }
    )


async def browser_scroll(
    manager,
    delta_x: int = 0,
    delta_y: int = 800,
    x: int = 0,
    y: int = 0,
    tab_target_id: str = "",
    tab_index: int = -1,
) -> str:
    page = await _current_page(
        manager,
        target_id=tab_target_id,
        index=None if tab_index < 0 else tab_index,
    )
    mouse = await _page_mouse(page)
    await mouse.scroll(x=x, y=y, delta_x=delta_x, delta_y=delta_y)
    await _sleep_after_action(0.2)
    _invalidate_dom_cache(manager, "browser_scroll")
    return _json_output(
        {
            "status": "ok",
            "delta_x": delta_x,
            "delta_y": delta_y,
            "page": await manager.page_metadata(page),
            "cache_invalidated": True,
        }
    )


async def browser_wait(
    manager,
    seconds: float = 0,
    selector: str = "",
    text: str = "",
    url_contains: str = "",
    timeout_seconds: float = 10,
    tab_target_id: str = "",
    tab_index: int = -1,
) -> str:
    page = await _current_page(
        manager,
        target_id=tab_target_id,
        index=None if tab_index < 0 else tab_index,
    )

    if seconds > 0 and not selector and not text and not url_contains:
        await asyncio.sleep(seconds)
        return _json_output({"status": "ok", "waited_seconds": seconds})

    deadline = asyncio.get_running_loop().time() + max(timeout_seconds, 0.2)
    while asyncio.get_running_loop().time() < deadline:
        if selector:
            exists = await page.evaluate("(selector) => !!document.querySelector(selector)", selector)
            if str(exists).lower() == "true":
                return _json_output({"status": "ok", "selector": selector})
        if text:
            matched_ref = await page.evaluate(
                _MATCH_REF_SCRIPT,
                _INTERACTIVE_SELECTOR,
                text,
                False,
                False,
            )
            if str(matched_ref or "").strip():
                return _json_output({"status": "ok", "text": text, "ref": str(matched_ref).strip()})
        if url_contains:
            current_url = await page.get_url()
            if url_contains in current_url:
                return _json_output({"status": "ok", "url": current_url})
        await asyncio.sleep(0.25)

    raise TimeoutError("Timed out waiting for the requested browser condition.")


async def browser_find(
    manager,
    text: str = "",
    role: str = "",
    label: str = "",
    limit: int = 20,
    tab_target_id: str = "",
    tab_index: int = -1,
) -> str:
    page = await _current_page(
        manager,
        target_id=tab_target_id,
        index=None if tab_index < 0 else tab_index,
    )
    snapshot_raw = await page.evaluate(_SNAPSHOT_SCRIPT, _INTERACTIVE_SELECTOR, 500)
    snapshot = _decode_json_string(snapshot_raw)
    elements = snapshot.get("elements", []) if isinstance(snapshot, dict) else []
    text_query = _normalize_match_text(text)
    label_query = _normalize_match_text(label)
    role_query = role.strip().lower()
    matches = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        blob = _element_blob(element)
        if text_query and text_query not in blob:
            continue
        if label_query and not _matches_label_query(element, label_query):
            continue
        if role_query and role_query != str(element.get("role", "")).lower():
            continue
        matches.append(_compact_field_candidate(element))
        if len(matches) >= max(1, min(limit, 100)):
            break
    return _json_output({"status": "ok", "count": len(matches), "matches": matches, "page": await manager.page_metadata(page)})


async def browser_get_element(
    manager,
    ref: str,
    fields: list[str] | None = None,
    tab_target_id: str = "",
    tab_index: int = -1,
) -> str:
    page = await _current_page(
        manager,
        target_id=tab_target_id,
        index=None if tab_index < 0 else tab_index,
    )
    normalized_ref = str(ref or "").strip()
    if not normalized_ref:
        raise ValueError("Provide ref from browser_snapshot.")
    cached = await manager.resolve_dom_snapshot_ref(page, normalized_ref)
    if isinstance(cached, dict) and cached.get("stale"):
        return _json_output(
            {
                "status": "error",
                "reason_code": "stale_ref",
                "needs_snapshot": True,
                "ref": normalized_ref,
                "error": "The cached ref belongs to a different page state. Call browser_snapshot again.",
            }
        )
    element, resolved_selector = await _resolve_element(manager, page, ref=normalized_ref)
    current = await _element_metadata(page, resolved_selector)
    requested = {str(item).strip() for item in (fields or []) if str(item).strip()}
    payload: dict[str, Any] = {
        "status": "ok",
        "ref": normalized_ref,
        "selector": resolved_selector,
        "cached": cached or {},
        "current": current or {},
        "page": await manager.page_metadata(page),
    }
    if requested:
        payload["cached"] = {
            key: value
            for key, value in (payload["cached"].get("metadata", payload["cached"]) if isinstance(payload["cached"], dict) else {}).items()
            if key in requested
        }
        payload["current"] = {
            key: value
            for key, value in (current or {}).items()
            if key in requested
        }
    return _json_output(payload)


async def _wait_for_fetch_selector(page: Any, selector: str, timeout_ms: int) -> None:
    if not selector:
        return
    deadline = asyncio.get_running_loop().time() + max(1.0, min(float(timeout_ms or 30000) / 1000, 120.0))
    while asyncio.get_running_loop().time() < deadline:
        try:
            exists = await page.evaluate("(selector) => !!document.querySelector(selector)", selector)
            if exists:
                return
        except Exception:
            pass
        await asyncio.sleep(0.25)
    raise TimeoutError(f"Timed out waiting for selector '{selector}'.")


async def _browser_fetch_rendered(
    manager,
    *,
    url: str,
    fetcher: str,
    selector: str,
    output: str,
    max_chars: int,
    wait_selector: str,
    network_idle: bool,
    timeout_ms: int,
    stealth: bool,
) -> dict[str, Any]:
    normalized_url = _normalize_fetch_url(url)
    browser = await manager.ensure_browser("auto")
    page = None
    try:
        if stealth:
            await _install_stealth_init(manager, browser)
        page = await browser.new_page(normalized_url)
        wait_state = "networkidle" if network_idle else "domcontentloaded"
        try:
            await page.wait_for_load_state(wait_state, timeout=max(1000, min(int(timeout_ms or 30000), 120000)))
        except Exception:
            pass
        await _wait_for_fetch_selector(page, wait_selector, timeout_ms)
        markup = await page.evaluate("() => document.documentElement ? document.documentElement.outerHTML : (document.body ? document.body.innerHTML : '')")
        current_url = await page.get_url() if hasattr(page, "get_url") else normalized_url
        parsed = _ScraplingParsedPage(
            body=str(markup or "").encode("utf-8", errors="replace"),
            url=str(current_url or normalized_url),
            status=0,
            reason="",
            headers={"content-type": "text/html; charset=utf-8"},
            encoding="utf-8",
        )
        result = _render_fetch_response(
            parsed,
            url=normalized_url,
            fetcher=fetcher,
            selector=selector,
            output=output,
            max_chars=max_chars,
        )
        result["http_status"] = 0
        return result
    except ImportError as exc:
        return {
            "status": "error",
            "reason_code": "missing_browser_dependencies",
            "url": normalized_url,
            "error": str(exc),
            "setup": 'pip install "scrapling>=0.4.8,<0.5.0"',
        }
    except Exception as exc:
        return {
            "status": "error",
            "reason_code": "browser_render_failed",
            "url": normalized_url,
            "fetcher": fetcher,
            "error": str(exc),
        }
    finally:
        if page is not None:
            try:
                await browser.close_page(page)
            except Exception:
                pass


async def browser_fetch(
    manager,
    url: str,
    mode: str = "auto",
    selector: str = "",
    output: str = "text",
    max_chars: int = 20000,
    wait_selector: str = "",
    network_idle: bool = False,
    timeout_ms: int = 30000,
) -> str:
    result = await asyncio.to_thread(
        _browser_fetch_sync,
        manager,
        url=url,
        mode=mode,
        selector=selector,
        output=output,
        max_chars=max_chars,
        wait_selector=wait_selector,
        network_idle=network_idle,
        timeout_ms=timeout_ms,
    )
    if result.get("status") == "needs_browser_render":
        rendered = await _browser_fetch_rendered(
            manager,
            url=str(result.get("url") or url),
            fetcher="stealth" if str(mode or "").lower() == "stealth" else "dynamic",
            selector=selector,
            output=output,
            max_chars=max_chars,
            wait_selector=wait_selector,
            network_idle=network_idle,
            timeout_ms=timeout_ms,
            stealth=str(mode or "").lower() == "stealth",
        )
        if "fallback_reason" in result:
            rendered.setdefault("metadata", {})["fallback_reason"] = result["fallback_reason"]
        return _json_output(rendered)
    return _json_output(result)


async def browser_extract_text(
    manager,
    selector: str = "",
    max_chars: int = 20000,
    tab_target_id: str = "",
    tab_index: int = -1,
) -> str:
    page = await _current_page(
        manager,
        target_id=tab_target_id,
        index=None if tab_index < 0 else tab_index,
    )
    script = """
    (selector, maxChars) => {
      const root = selector ? document.querySelector(selector) : document.body;
      const text = root ? (root.innerText || root.textContent || '') : '';
      return text.slice(0, maxChars);
    }
    """
    text = await page.evaluate(script, selector, max(1, min(int(max_chars or 20000), 200000)))
    return _json_output({"status": "ok", "selector": selector, "text": str(text or ""), "page": await manager.page_metadata(page)})


async def browser_fill_form(
    manager,
    fields: list[dict[str, Any]],
    submit_ref: str = "",
    submit_selector: str = "",
    wait_for_text: str = "",
    timeout_seconds: float = 10,
    tab_target_id: str = "",
    tab_index: int = -1,
) -> str:
    results = []
    for field in fields or []:
        result = json.loads(
            await browser_type(
                manager,
                text=str(field.get("text", field.get("value", ""))),
                ref=str(field.get("ref", "")),
                selector=str(field.get("selector", "")),
                target_text=str(field.get("target_text", "")),
                exact_text=bool(field.get("exact_text", False)),
                submit=False,
                clear=bool(field.get("clear", True)),
                tab_target_id=tab_target_id,
                tab_index=tab_index,
                verify_value=bool(field.get("verify_value", True)),
            )
        )
        results.append(result)
        if result.get("status") != "ok":
            return _json_output({"status": "error", "filled": results, "error": "A form field failed verification."})
    if submit_ref or submit_selector:
        submit_result = json.loads(
            await browser_click(
                manager,
                ref=submit_ref,
                selector=submit_selector,
                wait_for_text=wait_for_text,
                timeout_seconds=timeout_seconds,
                tab_target_id=tab_target_id,
                tab_index=tab_index,
            )
        )
        return _json_output({"status": submit_result.get("status", "ok"), "filled": results, "submit": submit_result})
    return _json_output({"status": "ok", "filled": results})


async def browser_downloads(manager, action: str = "list") -> str:
    downloads_dir = Path(manager.config["downloads_dir"])
    downloads_dir.mkdir(parents=True, exist_ok=True)
    normalized = action.strip().lower()
    if normalized == "clear":
        removed = 0
        for item in downloads_dir.iterdir():
            if item.is_file():
                item.unlink(missing_ok=True)
                removed += 1
        return _json_output({"status": "ok", "action": "clear", "removed": removed, "downloads_dir": str(downloads_dir)})
    files = []
    for item in sorted(downloads_dir.iterdir(), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True):
        if item.is_file():
            stat = item.stat()
            files.append({"path": str(item), "name": item.name, "size": stat.st_size, "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()})
    return _json_output({"status": "ok", "action": "list", "downloads_dir": str(downloads_dir), "files": files})


async def browser_console(manager) -> str:
    return _json_output({"status": "ok", "messages": [], "note": "Console collection is not enabled for already-open pages; use browser_evaluate for page-side diagnostics."})


async def browser_network_summary(
    manager,
    limit: int = 50,
    tab_target_id: str = "",
    tab_index: int = -1,
) -> str:
    page = await _current_page(
        manager,
        target_id=tab_target_id,
        index=None if tab_index < 0 else tab_index,
    )
    script = """
    (limit) => performance.getEntriesByType('resource').slice(-limit).map((entry) => ({
      name: entry.name,
      initiatorType: entry.initiatorType,
      duration: Math.round(entry.duration),
      transferSize: entry.transferSize || 0
    }))
    """
    entries = await page.evaluate(script, max(1, min(int(limit or 50), 200)))
    return _json_output({"status": "ok", "resources": _decode_json_string(entries), "page": await manager.page_metadata(page)})


async def browser_screenshot(
    manager,
    ref: str = "",
    selector: str = "",
    format: str = "png",
    tab_target_id: str = "",
    tab_index: int = -1,
) -> str:
    page = await _current_page(
        manager,
        target_id=tab_target_id,
        index=None if tab_index < 0 else tab_index,
    )
    normalized_format = format.lower()
    if normalized_format not in {"png", "jpeg", "webp"}:
        raise ValueError("format must be png, jpeg, or webp.")

    prefix = "browser_page"
    if ref or selector:
        element, _ = await _resolve_element(manager, page, ref=ref, selector=selector)
        image_data = await element.screenshot(format=normalized_format)
        prefix = "browser_element"
    else:
        image_data = await page.screenshot(format=normalized_format)

    output_path, skipped_screenshot = _write_previewable_screenshot(
        image_data,
        manager.config["screenshots_dir"],
        prefix,
        normalized_format,
    )
    if skipped_screenshot:
        return _json_output({
            "status": "skipped",
            "reason_code": skipped_screenshot["reason_code"],
            "screenshot_skipped": skipped_screenshot,
            "format": normalized_format,
        })
    return _json_output({"status": "ok", "path": output_path, "format": normalized_format})


async def browser_full_page_screenshot(
    manager,
    url: str = "",
    format: str = "png",
    mode: str = "auto",
    profile_directory: str = "",
    tab_target_id: str = "",
    tab_index: int = -1,
    stealth: bool = True,
    declutter: bool = True,
    remove_fixed: bool = True,
    click_close: bool = True,
    fallback_proxy: str = "suggest",
) -> str:
    browser = await manager.ensure_browser(mode, profile_directory)
    page = await _current_page(
        manager,
        mode=mode,
        profile_directory=profile_directory,
        target_id=tab_target_id,
        index=None if tab_index < 0 else tab_index,
        create_if_missing=True,
    )
    normalized_format = format.lower()
    if normalized_format not in {"png", "jpeg", "webp"}:
        raise ValueError("format must be png, jpeg, or webp.")

    normalized_fallback = str(fallback_proxy or "suggest").strip().lower()
    if normalized_fallback not in {"none", "suggest", "archive_is", "smry_ai"}:
        raise ValueError("fallback_proxy must be none, suggest, archive_is, or smry_ai.")

    stealth_init: dict[str, Any] = {"installed": False}
    stealth_runtime: dict[str, Any] = {"applied": False}
    if stealth:
        stealth_init = await _install_stealth_init(manager, browser)

    if url.strip():
        await page.goto(url.strip())
        await _wait_after_navigation(page, "domcontentloaded", timeout_seconds=15)
        await _sleep_after_action(0.7)

    if stealth:
        stealth_runtime = await _apply_stealth_runtime(page)

    cleanup: dict[str, Any] = {"status": "skipped"}
    if declutter:
        cleanup = await _run_global_declutter(page, remove_fixed=remove_fixed, click_close=click_close)
        await _sleep_after_action(0.25)

    page_metadata = await manager.page_metadata(page)
    original_url = str(page_metadata.get("url") or url or "").strip()
    used_fallback = ""
    fallback_url = ""
    fallback_cleanup: dict[str, Any] | None = None
    needs_fallback = _cleanup_needs_fallback(cleanup)

    if normalized_fallback in {"archive_is", "smry_ai"} and needs_fallback and original_url:
        fallback_url = _fallback_proxy_url(original_url, normalized_fallback)
        if fallback_url:
            used_fallback = normalized_fallback
            await page.goto(fallback_url)
            await _wait_after_navigation(page, "domcontentloaded", timeout_seconds=15)
            await _sleep_after_action(0.8)
            if stealth:
                stealth_runtime = await _apply_stealth_runtime(page)
            if declutter:
                fallback_cleanup = await _run_global_declutter(page, remove_fixed=remove_fixed, click_close=click_close)
                await _sleep_after_action(0.25)
            page_metadata = await manager.page_metadata(page)

    take_screenshot = getattr(browser, "take_screenshot", None)
    if callable(take_screenshot):
        output_path = _screenshot_path(manager.config["screenshots_dir"], "browser_full_page", normalized_format)
        image_bytes = await take_screenshot(
            path=str(output_path),
            full_page=True,
            format=normalized_format,
        )
        if image_bytes and not output_path.exists():
            output_path.write_bytes(image_bytes)
        saved_path = str(output_path)
        skipped_screenshot = _discard_invalid_screenshot(saved_path)
    else:
        image_data = await page.screenshot(format=normalized_format)
        saved_path, skipped_screenshot = _write_previewable_screenshot(
            image_data,
            manager.config["screenshots_dir"],
            "browser_full_page",
            normalized_format,
        )

    payload: dict[str, Any] = {
        "status": "ok",
        "path": saved_path,
        "format": normalized_format,
        "full_page": True,
        "page": page_metadata,
        "stealth": {
            "requested": bool(stealth),
            "init": stealth_init,
            "runtime": stealth_runtime,
        },
        "declutter": cleanup,
        "fallback_proxy": {
            "requested": normalized_fallback,
            "used": used_fallback,
            "url": fallback_url,
            "suggestions": _fallback_suggestions(original_url) if normalized_fallback == "suggest" and needs_fallback else [],
        },
    }
    if fallback_cleanup is not None:
        payload["fallback_proxy"]["declutter"] = fallback_cleanup
    if skipped_screenshot:
        payload.pop("path", None)
        payload["status"] = "skipped"
        payload["reason_code"] = skipped_screenshot["reason_code"]
        payload["screenshot_skipped"] = skipped_screenshot
    if cleanup.get("cloudflare_detected") or cleanup.get("access_denied_detected"):
        payload["note"] = (
            "Cloudflare, captcha, or access-denied pages cannot be reliably bypassed. "
            "The tool applied stealth-oriented browser settings and returned fallback suggestions when requested."
        )
    return _json_output(payload)


async def browser_evaluate(
    manager,
    script: str,
    args: list[Any] | None = None,
    tab_target_id: str = "",
    tab_index: int = -1,
) -> str:
    page = await _current_page(
        manager,
        target_id=tab_target_id,
        index=None if tab_index < 0 else tab_index,
    )
    result = await page.evaluate(script, *(args or []))
    return _json_output(
        {
            "status": "ok",
            "result": _decode_json_string(result),
            "page": await manager.page_metadata(page),
        }
    )


def register_tools(registry, settings) -> None:
    manager = configure_browser_use_manager(settings)

    registry.extend(
        [
            {
                "name": "browser_session",
                "description": "Inspect, reset, stop, or switch the built-in browser-use session between managed and system Chrome modes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["status", "doctor", "list_profiles", "stop", "reset", "use_managed", "use_system"],
                            "default": "status",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["auto", "managed", "system"],
                            "default": "auto",
                        },
                        "profile_directory": {"type": "string", "default": ""},
                    },
                    "required": [],
                },
                "callable": lambda action="status", mode="auto", profile_directory="": browser_session(
                    manager,
                    action=action,
                    mode=mode,
                    profile_directory=profile_directory,
                ),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
            },
            {
                "name": "browser_open",
                "description": "Ensure a browser session exists, reusing an already-open matching tab before navigating the current tab unless reuse_existing is false.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "default": ""},
                        "mode": {"type": "string", "enum": ["auto", "managed", "system"], "default": "auto"},
                        "profile_directory": {"type": "string", "default": ""},
                        "new_tab": {"type": "boolean", "default": False},
                        "reuse_existing": {"type": "boolean", "default": True},
                        "wait_until": {"type": "string", "enum": ["domcontentloaded", "networkidle", "load"], "default": "domcontentloaded"},
                    },
                    "required": [],
                },
                "callable": lambda url="", mode="auto", profile_directory="", new_tab=False, reuse_existing=True, wait_until="domcontentloaded": browser_open(
                    manager,
                    url=url,
                    mode=mode,
                    profile_directory=profile_directory,
                    new_tab=new_tab,
                    reuse_existing=reuse_existing,
                    wait_until=wait_until,
                ),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
            },
            {
                "name": "browser_navigate",
                "description": "Navigate the current browser tab to a new URL.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "tab_target_id": {"type": "string", "default": ""},
                        "tab_index": {"type": "integer", "default": -1},
                        "wait_until": {"type": "string", "enum": ["domcontentloaded", "networkidle", "load"], "default": "domcontentloaded"},
                    },
                    "required": ["url"],
                },
                "callable": lambda url, tab_target_id="", tab_index=-1, wait_until="domcontentloaded": browser_navigate(
                    manager,
                    url=url,
                    tab_target_id=tab_target_id,
                    tab_index=tab_index,
                    wait_until=wait_until,
                ),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
            },
            {
                "name": "browser_back",
                "description": "Go back in the current browser tab.",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": lambda: browser_back(manager),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
            },
            {
                "name": "browser_forward",
                "description": "Go forward in the current browser tab.",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": lambda: browser_forward(manager),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
            },
            {
                "name": "browser_reload",
                "description": "Reload the current browser tab.",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": lambda: browser_reload(manager),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
            },
            {
                "name": "browser_tabs",
                "description": "List, create, switch, or close browser tabs in the current browser-use session.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["list", "new", "switch", "close"], "default": "list"},
                        "url": {"type": "string", "default": ""},
                        "target_id": {"type": "string", "default": ""},
                        "index": {"type": "integer", "default": -1},
                        "tab_index": {"type": "integer", "default": -1},
                        "mode": {"type": "string", "enum": ["auto", "managed", "system"], "default": "auto"},
                    },
                    "required": [],
                },
                "callable": lambda action="list", url="", target_id="", index=-1, tab_index=-1, mode="auto": browser_tabs(
                    manager,
                    action=action,
                    url=url,
                    target_id=target_id,
                    index=index,
                    tab_index=tab_index,
                    mode=mode,
                ),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
            },
            {
                "name": "browser_snapshot",
                "description": "Capture the current browser tab state and assign stable refs to visible interactive elements.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "include_screenshot": {"type": "boolean", "default": False},
                        "limit": {"type": "integer", "default": 40},
                        "mode": {"type": "string", "enum": ["auto", "managed", "system"], "default": "auto"},
                        "tab_target_id": {"type": "string", "default": ""},
                        "tab_index": {"type": "integer", "default": -1},
                        "engine": {"type": "string", "enum": ["auto", "enhanced", "legacy"], "default": "auto"},
                        "include_tree": {"type": "boolean", "default": False},
                        "include_scroll_info": {"type": "boolean", "default": True},
                        "include_hidden_hints": {"type": "boolean", "default": True},
                    },
                    "required": [],
                },
                "callable": lambda include_screenshot=False, limit=40, mode="auto", tab_target_id="", tab_index=-1, engine="auto", include_tree=False, include_scroll_info=True, include_hidden_hints=True: browser_snapshot(
                    manager,
                    include_screenshot=include_screenshot,
                    limit=limit,
                    mode=mode,
                    tab_target_id=tab_target_id,
                    tab_index=tab_index,
                    engine=engine,
                    include_tree=include_tree,
                    include_scroll_info=include_scroll_info,
                    include_hidden_hints=include_hidden_hints,
                ),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
            },
            {
                "name": "browser_click",
                "description": "Click a browser element by ref, selector, visible text, or absolute page coordinates.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ref": {"type": "string", "default": ""},
                        "selector": {"type": "string", "default": ""},
                        "text": {"type": "string", "default": ""},
                        "exact_text": {"type": "boolean", "default": False},
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                        "clicks": {"type": "integer", "default": 1},
                        "tab_target_id": {"type": "string", "default": ""},
                        "tab_index": {"type": "integer", "default": -1},
                        "wait_for_selector": {"type": "string", "default": ""},
                        "wait_for_text": {"type": "string", "default": ""},
                        "wait_for_url_contains": {"type": "string", "default": ""},
                        "expect_new_tab": {"type": "boolean", "default": False},
                        "timeout_seconds": {"type": "number", "default": 10},
                    },
                    "required": [],
                },
                "callable": lambda ref="", selector="", text="", exact_text=False, x=None, y=None, button="left", clicks=1, tab_target_id="", tab_index=-1, wait_for_selector="", wait_for_text="", wait_for_url_contains="", expect_new_tab=False, timeout_seconds=10: browser_click(
                    manager,
                    ref=ref,
                    selector=selector,
                    text=text,
                    exact_text=exact_text,
                    x=x,
                    y=y,
                    button=button,
                    clicks=clicks,
                    tab_target_id=tab_target_id,
                    tab_index=tab_index,
                    wait_for_selector=wait_for_selector,
                    wait_for_text=wait_for_text,
                    wait_for_url_contains=wait_for_url_contains,
                    expect_new_tab=expect_new_tab,
                    timeout_seconds=timeout_seconds,
                ),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
            },
            {
                "name": "browser_type",
                "description": "Fast-fill a browser input or editable element by stable ref, selector, or target_text. Uses direct DOM value insertion for long text and returns target metadata for verification.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "ref": {"type": "string", "default": ""},
                        "selector": {"type": "string", "default": ""},
                        "target_text": {"type": "string", "default": ""},
                        "exact_text": {"type": "boolean", "default": False},
                        "submit": {"type": "boolean", "default": False},
                        "clear": {"type": "boolean", "default": True},
                        "tab_target_id": {"type": "string", "default": ""},
                        "tab_index": {"type": "integer", "default": -1},
                        "verify_value": {"type": "boolean", "default": True},
                        "wait_for_text": {"type": "string", "default": ""},
                        "timeout_seconds": {"type": "number", "default": 10},
                    },
                    "required": ["text"],
                },
                "callable": lambda text, ref="", selector="", target_text="", exact_text=False, submit=False, clear=True, tab_target_id="", tab_index=-1, verify_value=True, wait_for_text="", timeout_seconds=10: browser_type(
                    manager,
                    text=text,
                    ref=ref,
                    selector=selector,
                    target_text=target_text,
                    exact_text=exact_text,
                    submit=submit,
                    clear=clear,
                    tab_target_id=tab_target_id,
                    tab_index=tab_index,
                    verify_value=verify_value,
                    wait_for_text=wait_for_text,
                    timeout_seconds=timeout_seconds,
                ),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
            },
            {
                "name": "browser_press",
                "description": "Send a key press to the active browser tab.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "tab_target_id": {"type": "string", "default": ""},
                        "tab_index": {"type": "integer", "default": -1},
                    },
                    "required": ["key"],
                },
                "callable": lambda key, tab_target_id="", tab_index=-1: browser_press(
                    manager,
                    key=key,
                    tab_target_id=tab_target_id,
                    tab_index=tab_index,
                ),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
            },
            {
                "name": "browser_select_option",
                "description": "Select one or more option values in a browser select element.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "values": {"type": "array", "items": {"type": "string"}},
                        "ref": {"type": "string", "default": ""},
                        "selector": {"type": "string", "default": ""},
                        "tab_target_id": {"type": "string", "default": ""},
                        "tab_index": {"type": "integer", "default": -1},
                    },
                    "required": ["values"],
                },
                "callable": lambda values, ref="", selector="", tab_target_id="", tab_index=-1: browser_select_option(
                    manager,
                    values=values,
                    ref=ref,
                    selector=selector,
                    tab_target_id=tab_target_id,
                    tab_index=tab_index,
                ),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
            },
            {
                "name": "browser_scroll",
                "description": "Scroll the active browser tab by pixel deltas.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "delta_x": {"type": "integer", "default": 0},
                        "delta_y": {"type": "integer", "default": 800},
                        "x": {"type": "integer", "default": 0},
                        "y": {"type": "integer", "default": 0},
                        "tab_target_id": {"type": "string", "default": ""},
                        "tab_index": {"type": "integer", "default": -1},
                    },
                    "required": [],
                },
                "callable": lambda delta_x=0, delta_y=800, x=0, y=0, tab_target_id="", tab_index=-1: browser_scroll(
                    manager,
                    delta_x=delta_x,
                    delta_y=delta_y,
                    x=x,
                    y=y,
                    tab_target_id=tab_target_id,
                    tab_index=tab_index,
                ),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
            },
            {
                "name": "browser_wait",
                "description": "Wait for time to pass or for a browser condition such as selector, visible text, or URL match.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "seconds": {"type": "number", "default": 0},
                        "selector": {"type": "string", "default": ""},
                        "text": {"type": "string", "default": ""},
                        "url_contains": {"type": "string", "default": ""},
                        "timeout_seconds": {"type": "number", "default": 10},
                        "tab_target_id": {"type": "string", "default": ""},
                        "tab_index": {"type": "integer", "default": -1},
                    },
                    "required": [],
                },
                "callable": lambda seconds=0, selector="", text="", url_contains="", timeout_seconds=10, tab_target_id="", tab_index=-1: browser_wait(
                    manager,
                    seconds=seconds,
                    selector=selector,
                    text=text,
                    url_contains=url_contains,
                    timeout_seconds=timeout_seconds,
                    tab_target_id=tab_target_id,
                    tab_index=tab_index,
                ),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
            },
            {
                "name": "browser_find",
                "description": "Find visible browser elements by text, label, or role and return compact refs/metadata.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "default": ""},
                        "role": {"type": "string", "default": ""},
                        "label": {"type": "string", "default": ""},
                        "limit": {"type": "integer", "default": 20},
                        "tab_target_id": {"type": "string", "default": ""},
                        "tab_index": {"type": "integer", "default": -1},
                    },
                    "required": [],
                },
                "callable": lambda text="", role="", label="", limit=20, tab_target_id="", tab_index=-1: browser_find(
                    manager,
                    text=text,
                    role=role,
                    label=label,
                    limit=limit,
                    tab_target_id=tab_target_id,
                    tab_index=tab_index,
                ),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
                "metadata": {"observation": True, "mutates_state": False, "risk_level": "low"},
            },
            {
                "name": "browser_get_element",
                "description": "Inspect one browser_snapshot ref and return cached enhanced DOM metadata plus current element state.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ref": {"type": "string"},
                        "fields": {"type": "array", "items": {"type": "string"}, "default": []},
                        "tab_target_id": {"type": "string", "default": ""},
                        "tab_index": {"type": "integer", "default": -1},
                    },
                    "required": ["ref"],
                },
                "callable": lambda ref, fields=None, tab_target_id="", tab_index=-1: browser_get_element(
                    manager,
                    ref=ref,
                    fields=fields,
                    tab_target_id=tab_target_id,
                    tab_index=tab_index,
                ),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
                "metadata": {"observation": True, "mutates_state": False, "risk_level": "low"},
            },
            {
                "name": "browser_fetch",
                "description": (
                    "Read-only fetch and extract a URL with Scrapling. Use for known URLs before opening a browser; "
                    "supports HTTP, dynamic rendered, and explicit stealth modes."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "mode": {"type": "string", "enum": ["auto", "http", "dynamic", "stealth"], "default": "auto"},
                        "selector": {"type": "string", "default": ""},
                        "output": {"type": "string", "enum": ["text", "markdown", "html", "links", "metadata"], "default": "text"},
                        "max_chars": {"type": "integer", "default": 20000},
                        "wait_selector": {"type": "string", "default": ""},
                        "network_idle": {"type": "boolean", "default": False},
                        "timeout_ms": {"type": "integer", "default": 30000},
                    },
                    "required": ["url"],
                },
                "callable": lambda url, mode="auto", selector="", output="text", max_chars=20000, wait_selector="", network_idle=False, timeout_ms=30000: browser_fetch(
                    manager,
                    url=url,
                    mode=mode,
                    selector=selector,
                    output=output,
                    max_chars=max_chars,
                    wait_selector=wait_selector,
                    network_idle=network_idle,
                    timeout_ms=timeout_ms,
                ),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
                "metadata": {
                    "observation": True,
                    "mutates_state": False,
                    "risk_level": "low",
                    "parallel_safe": True,
                    "resource_locks": [],
                    "repeat_safe": True,
                },
            },
            {
                "name": "browser_extract_text",
                "description": "Extract visible text from the page or a CSS selector.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "selector": {"type": "string", "default": ""},
                        "max_chars": {"type": "integer", "default": 20000},
                        "tab_target_id": {"type": "string", "default": ""},
                        "tab_index": {"type": "integer", "default": -1},
                    },
                    "required": [],
                },
                "callable": lambda selector="", max_chars=20000, tab_target_id="", tab_index=-1: browser_extract_text(
                    manager,
                    selector=selector,
                    max_chars=max_chars,
                    tab_target_id=tab_target_id,
                    tab_index=tab_index,
                ),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
                "metadata": {"observation": True, "mutates_state": False, "risk_level": "low"},
            },
            {
                "name": "browser_fill_form",
                "description": "Fill multiple browser fields by refs/selectors and optionally click a submit control.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "fields": {"type": "array", "items": {"type": "object"}},
                        "submit_ref": {"type": "string", "default": ""},
                        "submit_selector": {"type": "string", "default": ""},
                        "wait_for_text": {"type": "string", "default": ""},
                        "timeout_seconds": {"type": "number", "default": 10},
                        "tab_target_id": {"type": "string", "default": ""},
                        "tab_index": {"type": "integer", "default": -1},
                    },
                    "required": ["fields"],
                },
                "callable": lambda fields, submit_ref="", submit_selector="", wait_for_text="", timeout_seconds=10, tab_target_id="", tab_index=-1: browser_fill_form(
                    manager,
                    fields=fields,
                    submit_ref=submit_ref,
                    submit_selector=submit_selector,
                    wait_for_text=wait_for_text,
                    timeout_seconds=timeout_seconds,
                    tab_target_id=tab_target_id,
                    tab_index=tab_index,
                ),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
            },
            {
                "name": "browser_downloads",
                "description": "List or clear files in the managed browser downloads directory.",
                "parameters": {
                    "type": "object",
                    "properties": {"action": {"type": "string", "enum": ["list", "clear"], "default": "list"}},
                    "required": [],
                },
                "callable": lambda action="list": browser_downloads(manager, action=action),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
            },
            {
                "name": "browser_console",
                "description": "Read collected browser console diagnostics when available.",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": lambda: browser_console(manager),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
            },
            {
                "name": "browser_network_summary",
                "description": "Summarize recent browser resource timing entries for the active tab.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "default": 50},
                        "tab_target_id": {"type": "string", "default": ""},
                        "tab_index": {"type": "integer", "default": -1},
                    },
                    "required": [],
                },
                "callable": lambda limit=50, tab_target_id="", tab_index=-1: browser_network_summary(
                    manager,
                    limit=limit,
                    tab_target_id=tab_target_id,
                    tab_index=tab_index,
                ),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
            },
            {
                "name": "browser_screenshot",
                "description": "Capture a screenshot of the current browser tab or a specific element ref/selector.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ref": {"type": "string", "default": ""},
                        "selector": {"type": "string", "default": ""},
                        "format": {"type": "string", "enum": ["png", "jpeg", "webp"], "default": "png"},
                        "tab_target_id": {"type": "string", "default": ""},
                        "tab_index": {"type": "integer", "default": -1},
                    },
                    "required": [],
                },
                "callable": lambda ref="", selector="", format="png", tab_target_id="", tab_index=-1: browser_screenshot(
                    manager,
                    ref=ref,
                    selector=selector,
                    format=format,
                    tab_target_id=tab_target_id,
                    tab_index=tab_index,
                ),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
            },
            {
                "name": "browser_full_page_screenshot",
                "description": (
                    "Capture a full-page browser screenshot. Optionally navigates to a URL first, "
                    "applies stealth-oriented init/runtime scripts, removes common fixed banners and popups, "
                    "and reports third-party archive/proxy fallbacks when the page remains blocked."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "default": ""},
                        "format": {"type": "string", "enum": ["png", "jpeg", "webp"], "default": "png"},
                        "mode": {"type": "string", "enum": ["auto", "managed", "system"], "default": "auto"},
                        "profile_directory": {"type": "string", "default": ""},
                        "tab_target_id": {"type": "string", "default": ""},
                        "tab_index": {"type": "integer", "default": -1},
                        "stealth": {"type": "boolean", "default": True},
                        "declutter": {"type": "boolean", "default": True},
                        "remove_fixed": {"type": "boolean", "default": True},
                        "click_close": {"type": "boolean", "default": True},
                        "fallback_proxy": {
                            "type": "string",
                            "enum": ["none", "suggest", "archive_is", "smry_ai"],
                            "default": "suggest",
                            "description": (
                                "Use 'suggest' to return fallback URLs. Use archive_is or smry_ai only when "
                                "sending the current URL to that third-party proxy is acceptable."
                            ),
                        },
                    },
                    "required": [],
                },
                "callable": lambda url="", format="png", mode="auto", profile_directory="", tab_target_id="", tab_index=-1, stealth=True, declutter=True, remove_fixed=True, click_close=True, fallback_proxy="suggest": browser_full_page_screenshot(
                    manager,
                    url=url,
                    format=format,
                    mode=mode,
                    profile_directory=profile_directory,
                    tab_target_id=tab_target_id,
                    tab_index=tab_index,
                    stealth=stealth,
                    declutter=declutter,
                    remove_fixed=remove_fixed,
                    click_close=click_close,
                    fallback_proxy=fallback_proxy,
                ),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
            },
            {
                "name": "browser_evaluate",
                "description": "Run JavaScript in the active browser tab. The script must be an arrow function like `(args) => ...`.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "script": {"type": "string"},
                        "args": {"type": "array", "items": {}},
                        "tab_target_id": {"type": "string", "default": ""},
                        "tab_index": {"type": "integer", "default": -1},
                    },
                    "required": ["script"],
                },
                "callable": lambda script, args=None, tab_target_id="", tab_index=-1: browser_evaluate(
                    manager,
                    script=script,
                    args=args,
                    tab_target_id=tab_target_id,
                    tab_index=tab_index,
                ),
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
            },
        ]
    )
