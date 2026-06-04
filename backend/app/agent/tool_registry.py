"""Tool registry container and shared tool-call helpers."""

from __future__ import annotations

import json
import re
import threading

DOMAINS = {"general", "desktop", "filesystem", "interaction", "browser"}
EXECUTION_MODES = {"async", "sync_stateless", "sync_thread_affine"}

_NEVER_PARALLEL = {
    "exec",
    "computer_functions_act",
    "computer_functions_activate_window",
    "computer_functions_clipboard",
    "computer_functions_kill_process",
    "system_volume",
    "screen_brightness",
    "desktop_configuration",
    "fs_write",
    "fs_append",
    "fs_patch",
    "fs_mkdir",
    "fs_copy",
    "fs_move",
    "fs_rename",
    "fs_delete",
    "fs_copy_many",
    "fs_move_many",
    "fs_delete_many",
    "explorer_open",
    "explorer_reveal",
    "open_folder",
    "select_in_explorer",
    "file_write",
    "file_append",
    "file_patch",
    "create_folder",
    "copy",
    "move",
    "rename",
    "delete",
    "computer_functions_get_window_state",
    "browser_session",
    "browser_open",
    "browser_navigate",
    "browser_back",
    "browser_forward",
    "browser_reload",
    "browser_tabs",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_press",
    "browser_select_option",
    "browser_scroll",
    "browser_wait",
    "browser_screenshot",
    "browser_full_page_screenshot",
    "browser_evaluate",
    "sanction_background_check",
}
_PARALLEL_SAFE = {
    "web_search",
    "calculator",
    "datetime",
    "memory_search",
    "memory_get",
    "fs_stat",
    "fs_list",
    "fs_tree",
    "fs_read",
    "fs_search",
    "fs_hash",
    "file_reader",
    "file_stat",
    "file_exists",
    "file_list",
    "file_tree",
    "file_glob",
    "file_read",
    "file_search",
    "file_hash",
}


def _tool_metadata(name: str, tool_lookup=None) -> dict:
    if callable(tool_lookup):
        tool = tool_lookup(name)
        if isinstance(tool, dict):
            return tool.get("metadata", {}) or {}
    if name in _NEVER_PARALLEL:
        return {"parallel_safe": False, "resource_locks": [name], "mutates_state": True}
    if name in _PARALLEL_SAFE:
        return {"parallel_safe": True, "resource_locks": [], "mutates_state": False}
    return {}


def can_parallelize(tool_calls: list[dict], tool_lookup=None) -> list[list[dict]]:
    """Group tool calls into batches that can execute safely in parallel."""
    if len(tool_calls) <= 1:
        return [tool_calls] if tool_calls else []

    names = {tc.get("name", "") for tc in tool_calls}
    if tool_lookup is None and names & _NEVER_PARALLEL:
        return [[tc] for tc in tool_calls]
    if tool_lookup is not None and any(
        not bool(_tool_metadata(tc.get("name", ""), tool_lookup).get("parallel_safe", tc.get("name", "") in _PARALLEL_SAFE))
        for tc in tool_calls
    ):
        return [[tc] for tc in tool_calls]

    parallel_batch: list[dict] = []
    sequential: list[list[dict]] = []
    seen_paths: set[str] = set()
    locked_resources: set[str] = set()

    for tc in tool_calls:
        name = tc.get("name", "")
        args = tc.get("arguments", {})
        metadata = _tool_metadata(name, tool_lookup)
        resource_locks = [str(item) for item in (metadata.get("resource_locks") or []) if str(item)]
        parallel_safe = bool(metadata.get("parallel_safe", name in _PARALLEL_SAFE))
        path = (
            args.get("path")
            or args.get("source")
            or args.get("destination")
            or args.get("workdir")
            or ""
        )

        if not parallel_safe or any(lock in locked_resources for lock in resource_locks):
            sequential.append([tc])
        elif name in _PARALLEL_SAFE or parallel_safe:
            parallel_batch.append(tc)
            locked_resources.update(resource_locks)
        elif path and path in seen_paths:
            sequential.append([tc])
        else:
            if path:
                seen_paths.add(path)
            locked_resources.update(resource_locks)
            parallel_batch.append(tc)

    batches: list[list[dict]] = []
    if parallel_batch:
        batches.append(parallel_batch)
    batches.extend(sequential)
    return batches


def coerce_tool_args(args: dict, schema: dict) -> dict:
    """Coerce LLM-provided tool arguments to match the JSON Schema types."""
    properties = schema.get("properties", {})
    if isinstance(properties, dict):
        coerced = {key: value for key, value in dict(args).items() if key in properties}
    else:
        coerced = dict(args)
    for key, value in list(coerced.items()):
        prop_schema = properties.get(key, {})
        expected = prop_schema.get("type")
        if expected is None:
            continue
        try:
            if expected == "integer" and isinstance(value, str):
                coerced[key] = int(value)
            elif expected == "number" and isinstance(value, str):
                coerced[key] = float(value)
            elif expected == "boolean" and isinstance(value, str):
                coerced[key] = value.lower() in {"true", "1", "yes"}
            elif expected == "boolean" and isinstance(value, (int, float)):
                coerced[key] = bool(value)
            elif expected == "string" and not isinstance(value, str):
                coerced[key] = str(value)
            elif expected == "array" and isinstance(value, str):
                coerced[key] = json.loads(value)
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    return coerced


def validate_tool_args(args: dict, schema: dict) -> list[str]:
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = schema.get("required", []) if isinstance(schema, dict) else []
    errors: list[str] = []
    for key in required or []:
        if key not in args:
            errors.append(f"Missing required argument '{key}'.")
    for key, value in args.items():
        prop_schema = properties.get(key, {}) if isinstance(properties, dict) else {}
        expected = prop_schema.get("type")
        if expected == "integer" and not isinstance(value, int):
            errors.append(f"Argument '{key}' must be an integer.")
        elif expected == "number" and not isinstance(value, (int, float)):
            errors.append(f"Argument '{key}' must be a number.")
        elif expected == "boolean" and not isinstance(value, bool):
            errors.append(f"Argument '{key}' must be a boolean.")
        elif expected == "string" and not isinstance(value, str):
            errors.append(f"Argument '{key}' must be a string.")
        elif expected == "array" and not isinstance(value, list):
            errors.append(f"Argument '{key}' must be an array.")
        elif expected == "object" and not isinstance(value, dict):
            errors.append(f"Argument '{key}' must be an object.")
        enum_values = prop_schema.get("enum")
        if enum_values and value not in enum_values:
            errors.append(f"Argument '{key}' must be one of: {', '.join(map(str, enum_values))}.")
    return errors


def _search_tokens(value: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9_+-]+", str(value or ""))
        if len(token) > 1
    }


class ToolRegistry:
    """Thread-safe registry populated by the skill loader."""

    def __init__(self, tools: list[dict] | None = None) -> None:
        self._tools: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._revision = 0
        if tools:
            self.extend(tools)

    def register(self, tool: dict) -> None:
        with self._lock:
            normalized = dict(tool)
            normalized.setdefault("visible_to_model", True)
            metadata = dict(normalized.get("metadata", {}) or {})
            name = str(normalized.get("name", ""))
            if "parallel_safe" not in metadata:
                metadata["parallel_safe"] = name in _PARALLEL_SAFE
            if "resource_locks" not in metadata:
                metadata["resource_locks"] = [name] if name in _NEVER_PARALLEL else []
            normalized["metadata"] = metadata
            self._tools[normalized["name"]] = normalized
            self._revision += 1

    def extend(self, tools: list[dict]) -> None:
        for tool in tools:
            self.register(tool)

    def get_all_tools(self, *, visible_only: bool = False) -> list[dict]:
        with self._lock:
            tools = list(self._tools.values())
            if visible_only:
                tools = [tool for tool in tools if tool.get("visible_to_model", True)]
            return tools

    def get_tool(self, name: str) -> dict | None:
        with self._lock:
            return self._tools.get(name)

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def remove_where(self, predicate) -> int:
        with self._lock:
            names = [name for name, tool in self._tools.items() if predicate(tool)]
            for name in names:
                self._tools.pop(name, None)
            if names:
                self._revision += 1
            return len(names)

    def replace_where(self, predicate, tools: list[dict]) -> None:
        with self._lock:
            names = [name for name, tool in self._tools.items() if predicate(tool)]
            for name in names:
                self._tools.pop(name, None)
            for tool in tools:
                normalized = dict(tool)
                normalized.setdefault("visible_to_model", True)
                metadata = dict(normalized.get("metadata", {}) or {})
                name = str(normalized.get("name", ""))
                if "parallel_safe" not in metadata:
                    metadata["parallel_safe"] = name in _PARALLEL_SAFE
                if "resource_locks" not in metadata:
                    metadata["resource_locks"] = [name] if name in _NEVER_PARALLEL else []
                normalized["metadata"] = metadata
                self._tools[normalized["name"]] = normalized
            if names or tools:
                self._revision += 1

    def set_visibility(self, names: list[str], visible: bool = True) -> list[str]:
        changed: list[str] = []
        wanted = {str(name) for name in names if str(name)}
        with self._lock:
            for name in wanted:
                tool = self._tools.get(name)
                if tool is None:
                    continue
                if bool(tool.get("visible_to_model", True)) == bool(visible):
                    continue
                tool["visible_to_model"] = bool(visible)
                changed.append(name)
            if changed:
                self._revision += 1
        return sorted(changed)

    def search_tools(
        self,
        query: str,
        *,
        limit: int = 8,
        include_hidden: bool = True,
    ) -> list[dict]:
        query_tokens = _search_tokens(query)
        query_text = str(query or "").strip().lower()
        with self._lock:
            tools = list(self._tools.values())

        matches: list[tuple[int, str, dict]] = []
        for tool in tools:
            visible = bool(tool.get("visible_to_model", True))
            if not include_hidden and not visible:
                continue
            name = str(tool.get("name") or "")
            if name == "tool_search":
                continue
            description = str(tool.get("description") or "")
            domain = str(tool.get("domain") or "")
            metadata = tool.get("metadata", {}) if isinstance(tool.get("metadata"), dict) else {}
            tags = metadata.get("search_tags") or []
            tags_text = " ".join(str(t) for t in tags) if isinstance(tags, list) else str(tags)
            searchable = " ".join(
                [
                    name,
                    name.replace("_", " "),
                    description,
                    domain,
                    str(metadata.get("skill") or ""),
                    tags_text,
                ]
            )
            haystack = searchable.lower()
            tokens = _search_tokens(searchable)
            score = len(query_tokens & tokens) * 10
            if query_text and query_text in haystack:
                score += 25
            if any(token in name.lower() for token in query_tokens):
                score += 8
            if score <= 0:
                continue
            if not visible:
                score += 2
            matches.append(
                (
                    score,
                    name,
                    {
                        "name": name,
                        "description": description,
                        "domain": domain,
                        "visible": visible,
                        "dynamic_load": bool(metadata.get("dynamic_load", False)),
                        "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
                    },
                )
            )
        matches.sort(key=lambda item: (-item[0], item[1]))
        return [match for _score, _name, match in matches[: max(1, min(50, int(limit or 8)))]]

    def dispatch(self, name: str, args: dict) -> dict | None:
        tool = self.get_tool(name)
        if tool is None:
            return None
        coerced = coerce_tool_args(args, tool.get("parameters", {}))
        validation_errors = validate_tool_args(coerced, tool.get("parameters", {}))
        return {"tool": tool, "arguments": coerced, "validation_errors": validation_errors}

    def get_schemas(self, *, visible_only: bool = True) -> list[dict]:
        with self._lock:
            tools = list(self._tools.values())
            if visible_only:
                tools = [tool for tool in tools if tool.get("visible_to_model", True)]
            return [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get(
                            "parameters", {"type": "object", "properties": {}}
                        ),
                    },
                }
                for t in tools
            ]
