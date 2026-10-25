from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from .tools import browser_fetch


def _configured_allowed_domains(settings: Any) -> list[str]:
    browser = getattr(settings, "browser", None)
    if isinstance(browser, dict):
        raw = browser.get("allowed_domains")
    else:
        raw = getattr(browser, "allowed_domains", None)
    return [str(item).strip() for item in (raw or []) if str(item).strip()]


def register_browser_tools(registry, settings) -> None:
    # Interactive browsing is provided by the built-in browser server that the
    # Monaw extension connects to (mcp_bridge/builtin.py, browser_use/extension);
    # this skill's only native tool is the HTTP fetcher.
    policy = SimpleNamespace(config={"allowed_domains": _configured_allowed_domains(settings)})

    registry.extend(
        [
            {
                "name": "browser_fetch",
                "description": (
                    "Read-only HTTP fetch of a known URL, extracted with Scrapling (no browser, no JavaScript). "
                    "Use it to read or summarize a page before opening a tab. If it returns "
                    "status \"needs_browser_render\", open the page with mcp__chrome-devtools__new_page "
                    "or navigate_page and read it with take_snapshot."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "mode": {
                            "type": "string",
                            "enum": ["auto", "http"],
                            "default": "auto",
                            "description": (
                                "auto: flag pages that look JavaScript-dependent or blocked; http: return the raw HTTP "
                                "result as is."
                            ),
                        },
                        "selector": {"type": "string", "default": ""},
                        "output": {"type": "string", "enum": ["text", "markdown", "html", "links", "metadata"], "default": "text"},
                        "max_chars": {"type": "integer", "default": 20000},
                        "timeout_ms": {"type": "integer", "default": 30000},
                    },
                    "required": ["url"],
                },
                "callable": lambda url, mode="auto", selector="", output="text", max_chars=20000, timeout_ms=30000, **_unused: browser_fetch(
                    policy,
                    url=url,
                    mode=mode,
                    selector=selector,
                    output=output,
                    max_chars=max_chars,
                    timeout_ms=timeout_ms,
                ),
                "domain": "browser",
                "execution_mode": "async",
                # Fetches are capped at 120 s; leave room for parsing.
                "timeout_seconds": 130.0,
                "metadata": {
                    "observation": True,
                    "mutates_state": False,
                    "risk_level": "low",
                    "parallel_safe": True,
                    "resource_locks": [],
                    "repeat_safe": True,
                },
            }
        ]
    )
