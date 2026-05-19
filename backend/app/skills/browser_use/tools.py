from __future__ import annotations

import asyncio
import base64
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

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
    ):
        value = element.get(key)
        if isinstance(value, list):
            values.extend(str(item) for item in value)
        elif value is not None:
            values.append(str(value))
    labels = element.get("labels")
    if isinstance(labels, list):
        values.extend(str(item) for item in labels)
    return " ".join(values).lower()


def _field_candidate_kind(element: dict[str, Any]) -> str:
    blob = _element_blob(element)
    hint = str(element.get("target_hint") or "").lower()
    role = str(element.get("role") or "").lower()
    tag = str(element.get("tag") or "").lower()
    is_editable = (
        tag in {"input", "textarea"}
        or role == "textbox"
        or str(element.get("contenteditable") or "").lower() in {"true", "plaintext-only"}
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
    y = int(element.get("y") or 0)
    return (-score, y, original_index)


def _compact_field_candidate(element: dict[str, Any]) -> dict[str, Any]:
    return {
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
    page,
    *,
    ref: str = "",
    selector: str = "",
    text: str = "",
    exact_text: bool = False,
    prefer_editable: bool = False,
):
    chosen_selector = selector.strip()
    if ref.strip():
        chosen_selector = _selector_for_ref(ref)

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
    page = await _current_page(
        manager,
        target_id=tab_target_id,
        index=None if tab_index < 0 else tab_index,
        create_if_missing=True,
    )
    await page.goto(url)
    await _wait_after_navigation(page, wait_until)
    await _sleep_after_action(0.8)
    return _json_output({"status": "ok", "page": await manager.page_metadata(page), "wait_until": wait_until})


async def browser_back(manager) -> str:
    page = await _current_page(manager)
    await page.go_back()
    await _sleep_after_action(0.6)
    return _json_output({"status": "ok", "page": await manager.page_metadata(page)})


async def browser_forward(manager) -> str:
    page = await _current_page(manager)
    await page.go_forward()
    await _sleep_after_action(0.6)
    return _json_output({"status": "ok", "page": await manager.page_metadata(page)})


async def browser_reload(manager) -> str:
    page = await _current_page(manager)
    await page.reload()
    await _sleep_after_action(0.8)
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
        return _json_output({"status": "ok", "page": await manager.page_metadata(page), "tabs": await manager.tabs()})
    if normalized_action == "switch":
        page = await manager.switch_tab(target_id=target_id, index=None if selected_index < 0 else selected_index)
        return _json_output({"status": "ok", "page": page, "tabs": await manager.tabs()})
    if normalized_action == "close":
        page = await manager.get_page(target_id=target_id, index=None if selected_index < 0 else selected_index, create_if_missing=False)
        if page is None:
            raise RuntimeError("No active tab to close.")
        await browser.close_page(page)
        await _sleep_after_action(0.3)
        return _json_output({"status": "ok", "tabs": await manager.tabs()})
    raise ValueError(f"Unsupported browser_tabs action '{action}'.")


async def browser_snapshot(
    manager,
    include_screenshot: bool = False,
    limit: int = 40,
    mode: str = "auto",
    tab_target_id: str = "",
    tab_index: int = -1,
) -> str:
    page = await _current_page(
        manager,
        mode=mode,
        target_id=tab_target_id,
        index=None if tab_index < 0 else tab_index,
    )
    requested_limit = max(1, min(limit, 200))
    collection_limit = max(requested_limit * 6, 240)
    snapshot_raw = await page.evaluate(
        _SNAPSHOT_SCRIPT,
        _INTERACTIVE_SELECTOR,
        min(collection_limit, 800),
    )
    snapshot = _prepare_snapshot(_decode_json_string(snapshot_raw), requested_limit)
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
    return _json_output(
        {
            "status": "ok" if verification.get("status") == "ok" else "error",
            "clicked": clicked,
            "target_after": target_after,
            "verification": verification,
            "page": await manager.page_metadata(page),
            "tabs_before": before_tabs,
            "tabs_after": tabs_after,
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
            page,
            ref=ref,
            selector=selector,
            exact_text=exact_text,
            prefer_editable=True,
        )
    else:
        element, resolved_selector = await _resolve_element(
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
        verification = {
            "status": "ok" if actual_value == text else "error",
            "verified": actual_value == text,
            "expected_value": text,
            "actual_value": actual_value,
            "reason_code": "" if actual_value == text else "value_mismatch",
        }
    if wait_for_text:
        verification = await _verify_browser_condition(page, text=wait_for_text, timeout_seconds=timeout_seconds)
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
    return _json_output({"status": "ok", "key": key, "page": await manager.page_metadata(page)})


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
    element, resolved_selector = await _resolve_element(page, ref=ref, selector=selector)
    chosen_values: list[str] = [value for value in values if str(value).strip()]
    if not chosen_values:
        raise ValueError("Provide at least one option value.")
    await element.select_option(chosen_values)
    await _sleep_after_action(0.2)
    return _json_output(
        {
            "status": "ok",
            "selector": resolved_selector,
            "values": chosen_values,
            "page": await manager.page_metadata(page),
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
    return _json_output(
        {
            "status": "ok",
            "delta_x": delta_x,
            "delta_y": delta_y,
            "page": await manager.page_metadata(page),
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
    query = " ".join(part for part in (text, label) if part).lower()
    role_query = role.strip().lower()
    matches = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        blob = _element_blob(element)
        if query and query not in blob:
            continue
        if role_query and role_query != str(element.get("role", "")).lower():
            continue
        matches.append(_compact_field_candidate(element))
        if len(matches) >= max(1, min(limit, 100)):
            break
    return _json_output({"status": "ok", "count": len(matches), "matches": matches, "page": await manager.page_metadata(page)})


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
        element, _ = await _resolve_element(page, ref=ref, selector=selector)
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
                    },
                    "required": [],
                },
                "callable": lambda include_screenshot=False, limit=40, mode="auto", tab_target_id="", tab_index=-1: browser_snapshot(
                    manager,
                    include_screenshot=include_screenshot,
                    limit=limit,
                    mode=mode,
                    tab_target_id=tab_target_id,
                    tab_index=tab_index,
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
