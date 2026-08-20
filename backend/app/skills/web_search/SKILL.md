---
name: web-search
display_name: Web search
summary: Search the web for current information when a live answer is needed.
description: Search the web for current information using the configured Tavily connection.
version: 1.0.0
enabled_by_default: true
tier: recommended
metadata:
  openclaw:
    requires:
      env:
        - TAVILY_API_KEY
---

Use `web_search` when the user needs current information, source discovery, or a live web lookup.

Rules:
- Prefer direct answers when the requested information is stable and already known.
- Include the relevant search result evidence in the final answer; do not claim to have searched if the tool was unavailable.
