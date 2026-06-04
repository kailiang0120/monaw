"""Policy decisions for tool-call validation and loop protection."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.agent.harness.tool_protocol import ToolCallRequest, ToolCallResult
from app.agent.tool_registry import ToolRegistry


@dataclass
class ToolPolicyDecision:
    allowed: bool
    reason: str = ""
    requires_approval: bool = False
    risk: str = "low"
    metadata: dict[str, Any] = field(default_factory=dict)


def tool_call_signature(name: str, arguments: dict) -> str:
    return json.dumps(
        {"name": name, "arguments": arguments},
        sort_keys=True,
        ensure_ascii=False,
    )


_OBSERVATION_TOOLS = {
    "browser_snapshot",
    "browser_screenshot",
    "browser_tabs",
    "browser_wait",
    "computer_functions_list_apps",
    "computer_functions_get_window",
    "computer_functions_get_window_state",
    "computer_functions_diagnose",
    "datetime",
    "calculator",
    "file_reader",
    "memory_get",
    "memory_search",
    "recall_memory",
    "web_search",
}

_HIGH_RISK_PREFIXES = ("mcp_",)
_HIGH_RISK_TOOLS = {
    "exec",
    "computer_functions_act",
    "computer_functions_kill_process",
    "browser_evaluate",
    "computer_functions_activate_window",
}
_MEDIUM_RISK_TOOLS = {
    "browser_click",
    "browser_type",
    "browser_press",
    "browser_select_option",
    "browser_scroll",
    "write_file",
    "append_file",
}

_STATE_AWARE_BROWSER_REPEAT_TOOLS = {
    "browser_open",
    "browser_navigate",
    "browser_back",
    "browser_forward",
    "browser_reload",
    "browser_click",
    "browser_type",
    "browser_press",
    "browser_select_option",
    "browser_scroll",
}


class ToolPolicy:
    def __init__(self, *, max_identical_calls: int = 2) -> None:
        self.max_identical_calls = max(1, max_identical_calls)
        self._call_counts: dict[str, int] = {}
        self._last_results: dict[str, ToolCallResult] = {}

    def begin_turn(self) -> None:
        self._call_counts.clear()
        self._last_results.clear()

    def decide(
        self,
        *,
        request: ToolCallRequest,
        registry: ToolRegistry,
        arguments: dict | None = None,
    ) -> ToolPolicyDecision:
        tool = registry.get_tool(request.name)
        if tool is None:
            return ToolPolicyDecision(
                allowed=False,
                reason=f"Unknown tool '{request.name}'",
                risk="unknown",
                metadata={"code": "unknown_tool"},
            )

        normalized_arguments = arguments if arguments is not None else request.arguments
        signature = tool_call_signature(request.name, normalized_arguments)
        repeat_count = self._call_counts.get(signature, 0)
        risk = self.classify_risk(request.name, tool)

        if repeat_count >= self.max_identical_calls and not self._is_repeat_safe(request.name, normalized_arguments, tool):
            return ToolPolicyDecision(
                allowed=False,
                reason="Repeated identical tool call blocked. Use prior result or choose another action.",
                risk=risk,
                metadata={
                    "code": "repeated_tool_call_blocked",
                    "signature": signature,
                    "repeat_count": repeat_count + 1,
                },
            )

        self._call_counts[signature] = repeat_count + 1
        return ToolPolicyDecision(
            allowed=True,
            risk=risk,
            requires_approval=self.requires_approval(request.name, tool),
            metadata={"signature": signature, "repeat_count": repeat_count + 1},
        )

    def record_result(self, request: ToolCallRequest, result: ToolCallResult, arguments: dict | None = None) -> None:
        signature = tool_call_signature(request.name, arguments if arguments is not None else request.arguments)
        self._last_results[signature] = result

    def classify_risk(self, tool_name: str, tool: dict | None = None) -> str:
        if tool:
            metadata_risk = str((tool.get("metadata", {}) or {}).get("risk_level") or "").strip().lower()
            if metadata_risk in {"low", "medium", "high"}:
                return metadata_risk
        if tool_name in _HIGH_RISK_TOOLS or tool_name.startswith(_HIGH_RISK_PREFIXES):
            return "high"
        if tool_name in _MEDIUM_RISK_TOOLS:
            return "medium"
        if tool and str(tool.get("domain") or "") in {"desktop", "interaction"}:
            return "high"
        if tool and str(tool.get("domain") or "") == "filesystem" and tool_name not in {"file_reader", "read_file"}:
            return "medium"
        return "low"

    def requires_approval(self, tool_name: str, tool: dict | None = None) -> bool:
        if tool_name in {"exec", "computer_functions_act", "computer_functions_kill_process"}:
            return True
        if tool and bool(tool.get("requires_approval", False)):
            return True
        return False

    @staticmethod
    def _is_repeat_safe(tool_name: str, arguments: dict, tool: dict | None = None) -> bool:
        metadata = (tool or {}).get("metadata", {}) or {}
        if bool(metadata.get("repeat_safe")):
            return True
        if bool(metadata.get("observation")):
            return True
        if metadata.get("mutates_state") is False and str(metadata.get("risk_level") or "low") == "low":
            return True
        if (
            tool_name in _STATE_AWARE_BROWSER_REPEAT_TOOLS
            and str((tool or {}).get("domain") or "") == "browser"
        ):
            return True
        if tool_name == "browser_tabs":
            action = str(arguments.get("action") or "list").strip().lower()
            return action == "list"
        return tool_name in _OBSERVATION_TOOLS
