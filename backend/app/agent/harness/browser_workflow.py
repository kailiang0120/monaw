"""Small, turn-local hints based on actual browser tool use, without replaying calls."""

from __future__ import annotations

import hashlib

from app.agent.tool_observations import DEVTOOLS_PREFIX, parse_tool_json


class BrowserWorkflow:
    def __init__(self) -> None:
        self._hinted: set[str] = set()
        self._last_snapshot: tuple[object, str] | None = None
        self._last_fill: tuple[object, object] | None = None
        self._scripts = 0

    def observe(self, name: str, arguments: dict, output: str, status: str) -> str:
        if not name.startswith(DEVTOOLS_PREFIX):
            # Another tool (including desktop or scripts) may change the page.
            self._last_snapshot = self._last_fill = None
            self._scripts = 0
            return ""
        short = name[len(DEVTOOLS_PREFIX):]
        payload = parse_tool_json(output)
        if status != "ok" or payload.get("is_error"):
            self._last_snapshot = self._last_fill = None
            self._scripts = 0
            if short in {"click", "fill", "fill_form", "type_text", "press_key", "evaluate_script"}:
                return self._hint("recovery", "A browser action failed or stopped partway through. Check its completion details before retrying. Inspect the existing target with inspect_element, use wait_for for a reported blocking condition, or take a fresh snapshot if the uid is stale. Do not repeat completed fields or submissions.")
            return ""

        page = arguments.get("pageId")
        if short in {"new_page", "navigate_page", "select_page", "close_page"}:
            self._last_snapshot = self._last_fill = None
        content = payload.get("content")
        text = "\n".join(str(item.get("text") or "") for item in (content if isinstance(content, list) else [])
                         if isinstance(item, dict) and item.get("type") == "text")
        marker = "## Latest page snapshot\n"
        snapshot = text.partition(marker)[2] if marker in text else ""
        hint = ""
        if snapshot:
            key = (page, hashlib.sha256(snapshot.encode()).hexdigest())
            if short == "take_snapshot" and key == self._last_snapshot:
                hint = self._hint("duplicate_read", "The latest snapshot repeats the preceding snapshot. Reuse its uids and page content. If waiting for a change, use wait_for with a specific condition instead of polling take_snapshot. A fresh read is still appropriate after navigation, external changes, or a stale target.")
            elif short not in {"take_snapshot", "wait_for"}:
                hint = self._hint("snapshot", "This browser result already includes an updated snapshot. Use it to verify the action and choose the next target; request another snapshot only if more recent or missing information is needed.")
            self._last_snapshot = key
        elif short not in {"inspect_element", "take_screenshot", "list_pages"}:
            self._last_snapshot = None

        if short == "fill":
            current = (page, arguments.get("uid"))
            if self._last_fill and self._last_fill[0] == page and self._last_fill[1] != current[1]:
                hint = self._hint("batch_fields", "For remaining independent fields already identified in this form, prefer one fill_form call and verify its final snapshot. Keep dependent fields and submission separate; do not refill completed fields just to batch them.") or hint
            self._last_fill = current
        elif short not in {"take_snapshot", "inspect_element"}:
            self._last_fill = None
        self._scripts = self._scripts + 1 if short == "evaluate_script" else 0
        if self._scripts >= 2:
            hint = self._hint("scripts", "Before generating another page script, check whether inspect_element can answer the DOM question, wait_for can handle readiness, or fill_form can perform the field updates. Use evaluate_script when the dedicated tools cannot express the task.") or hint
        return hint

    def _hint(self, kind: str, text: str) -> str:
        if kind in self._hinted or len(self._hinted) >= 4:
            return ""
        self._hinted.add(kind)
        return "Browser workflow guidance: " + text
