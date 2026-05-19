from __future__ import annotations

import ast
import json
import math
import operator
import os
from datetime import datetime, timezone

from app.skills.core import system_controls

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_MATH_NAMES = {key: value for key, value in math.__dict__.items() if not key.startswith("_")}


def _eval_math_node(node: ast.AST):
    if isinstance(node, ast.Expression):
        return _eval_math_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval_math_node(node.left), _eval_math_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_math_node(node.operand))
    if isinstance(node, ast.Name) and node.id in _MATH_NAMES and isinstance(_MATH_NAMES[node.id], (int, float)):
        return _MATH_NAMES[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _MATH_NAMES:
        fn = _MATH_NAMES[node.func.id]
        if not callable(fn):
            raise ValueError(f"'{node.func.id}' is not callable")
        return fn(*[_eval_math_node(arg) for arg in node.args])
    raise ValueError(f"Unsupported expression: {ast.dump(node, include_attributes=False)}")


def _calculator(expression: str) -> str:
    try:
        result = _eval_math_node(ast.parse(expression, mode="eval"))
        return str(result)
    except Exception as exc:
        return f"Error: {exc}"


def _datetime_tool() -> str:
    local_now = datetime.now().astimezone()
    utc_now = datetime.now(timezone.utc)
    return json.dumps(
        {
            "local": local_now.isoformat(),
            "timezone": local_now.tzname(),
            "utc": utc_now.isoformat(),
            "epoch_seconds": int(local_now.timestamp()),
        }
    )


def _build_web_search_tool(settings) -> dict | None:
    tavily_api_key = getattr(settings, "tavily_api_key", "")
    if not tavily_api_key:
        return None

    os.environ["TAVILY_API_KEY"] = tavily_api_key
    from langchain_tavily import TavilySearch

    search = TavilySearch(max_results=5)

    def _web_search(query: str) -> str:
        return str(search.invoke(query))

    return {
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


def register_tools(registry, settings) -> None:
    registry.extend(
        [
            {
                "name": "calculator",
                "description": "Evaluate a mathematical expression.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "expression": {
                            "type": "string",
                            "description": "Python-compatible math expression.",
                        }
                    },
                    "required": ["expression"],
                },
                "callable": _calculator,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            },
            {
                "name": "datetime",
                "description": "Get the current date and time.",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": _datetime_tool,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            },
            {
                "name": "system_volume",
                "description": "Get or adjust the Windows master output volume.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["get", "set", "increase", "decrease", "mute", "unmute", "toggle_mute"],
                            "default": "get",
                        },
                        "level": {"type": "integer", "default": 50, "description": "Target level from 0 to 100 for set."},
                        "step": {"type": "integer", "default": 5, "description": "Adjustment size from 1 to 100."},
                    },
                    "required": [],
                },
                "callable": system_controls.system_volume,
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": {"parallel_safe": False, "resource_locks": ["system_audio"], "mutates_state": True, "risk_level": "medium"},
            },
            {
                "name": "screen_brightness",
                "description": "Get or adjust the Windows display brightness when the monitor exposes WMI brightness controls.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["get", "set", "increase", "decrease"],
                            "default": "get",
                        },
                        "level": {"type": "integer", "default": 50, "description": "Target brightness from 0 to 100 for set."},
                        "step": {"type": "integer", "default": 10, "description": "Adjustment size from 1 to 100."},
                    },
                    "required": [],
                },
                "callable": system_controls.screen_brightness,
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": {"parallel_safe": False, "resource_locks": ["display_brightness"], "mutates_state": True, "risk_level": "medium"},
            },
            {
                "name": "desktop_configuration",
                "description": "Open common Windows Settings pages for display, sound, network, Bluetooth, power, notifications, apps, privacy, and date/time configuration.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["open", "list_pages"], "default": "open"},
                        "page": {
                            "type": "string",
                            "enum": sorted(system_controls._SETTINGS_PAGES),
                            "default": "settings",
                        },
                    },
                    "required": [],
                },
                "callable": system_controls.desktop_configuration,
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": {"parallel_safe": False, "resource_locks": ["desktop"], "mutates_state": True, "risk_level": "low"},
            },
        ]
    )

    web_search_tool = _build_web_search_tool(settings)
    if web_search_tool is not None:
        registry.register(web_search_tool)
