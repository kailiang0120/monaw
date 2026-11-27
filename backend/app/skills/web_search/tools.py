from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

_KEENABLE_URL = "https://api.keenable.ai/v1/search"
_KEENABLE_FAILURES = {
    401: "the API key was rejected; check KEENABLE_API_KEY",
    403: "the API key was rejected; check KEENABLE_API_KEY",
    402: "the Keenable account is out of credits",
    429: "Keenable is rate limiting requests; try again shortly",
}


def _keenable_search(api_key: str, query: str, max_results: int = 5) -> str:
    body = json.dumps(
        {"query": query, "max_results": max_results, "snippet_max_length": 500}
    ).encode()
    request = urllib.request.Request(
        _KEENABLE_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-API-Key": api_key,
            # Only the app name; nothing about the user or this computer.
            "X-Keenable-Title": "Monaw",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read(1_000_000))
    except urllib.error.HTTPError as exc:
        reason = _KEENABLE_FAILURES.get(exc.code, "try again later")
        return f"Error: Keenable search failed (HTTP {exc.code}): {reason}."
    except (urllib.error.URLError, TimeoutError, ValueError):
        return "Error: Keenable search is unavailable right now."
    results = []
    for item in (data.get("results") or [])[:max_results] if isinstance(data, dict) else []:
        if not isinstance(item, dict) or not str(item.get("url", "")).startswith("http"):
            continue
        results.append(
            {
                "title": item.get("title", ""),
                "url": item["url"],
                # `description` is always empty in Keenable's response; the text is `snippet`.
                "content": item.get("snippet") or item.get("description") or "",
            }
        )
    return json.dumps(results, ensure_ascii=False)


def register_tools(registry, settings) -> None:
    tavily_api_key = getattr(settings, "tavily_api_key", "")
    keenable_api_key = (getattr(settings, "keenable_api_key", "") or "").strip()
    if tavily_api_key:
        os.environ["TAVILY_API_KEY"] = tavily_api_key
        from langchain_tavily import TavilySearch

        search = TavilySearch(max_results=5)

        def _web_search(query: str) -> str:
            return str(search.invoke(query))

    elif keenable_api_key:
        # Optional alternative: used only when no Tavily key is set, so Tavily stays first.
        def _web_search(query: str) -> str:
            return _keenable_search(keenable_api_key, query)

    else:
        return

    registry.register(
        {
            "name": "web_search",
            "description": "Search the web for current information.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query."}},
                "required": ["query"],
            },
            "callable": _web_search,
            "domain": "general",
            "execution_mode": "sync_stateless",
            "affinity_group": None,
        }
    )
