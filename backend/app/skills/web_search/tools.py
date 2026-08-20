from __future__ import annotations

import os


def register_tools(registry, settings) -> None:
    tavily_api_key = getattr(settings, "tavily_api_key", "")
    if not tavily_api_key:
        return

    os.environ["TAVILY_API_KEY"] = tavily_api_key
    from langchain_tavily import TavilySearch

    search = TavilySearch(max_results=5)

    def _web_search(query: str) -> str:
        return str(search.invoke(query))

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
