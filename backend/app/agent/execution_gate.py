"""Central service for approval and access-grant wait/resume behavior."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from app.agent.access_grant_broker import (
    cleanup_resume as cleanup_grant_resume,
    get_grant_ticket,
    get_resume_decision as get_grant_resume_decision,
    grant_validation_error,
    register_pending_resume as register_grant_resume,
)
from app.agent.approval_broker import (
    cleanup_resume as cleanup_approval_resume,
    get_resume_decision as get_approval_resume_decision,
    get_ticket as get_approval_ticket,
    register_pending_resume as register_approval_resume,
)
from app.agent.execution_resume import resume_approved_ticket
from app.agent.iteration_budget import IterationBudget
from app.agent.run_context import (
    current_control_session_id,
    current_conversation_id,
    current_execution_source,
)
from app.agent.tool_registry import ToolRegistry

ToolExecute = Callable[[IterationBudget, dict[str, Any], dict[str, Any]], Awaitable[str]]


class ExecutionGateService:
    """Owns interactive gate detection, waiting, and exact-context resume."""

    def __init__(self, *, default_timeout_seconds: float = 600.0) -> None:
        self.default_timeout_seconds = default_timeout_seconds

    async def handle_pending_tool_output(
        self,
        *,
        tool_output: str,
        tool_name: str,
        arguments: dict[str, Any],
        registry: ToolRegistry,
        budget: IterationBudget,
        execute_tool: ToolExecute,
    ) -> tuple[str, list[dict[str, Any]]]:
        pending_event = self.check_pending_status(tool_output)
        if pending_event is None:
            return tool_output, []

        dispatched = registry.dispatch(tool_name, arguments)
        if dispatched is None:
            return tool_output, [pending_event]

        resolved_output = tool_output
        emitted_events: list[dict[str, Any]] = [pending_event]
        async for resolution_event in self.await_ticket_resolution(
            budget=budget,
            ticket_id=pending_event["data"]["ticket_id"],
            event_kind=pending_event["event"],
            tool_dict=dispatched["tool"],
            arguments=dispatched["arguments"],
            execute_tool=execute_tool,
        ):
            if "_result" in resolution_event:
                resolved_output = resolution_event["_result"]
            else:
                emitted_events.append(resolution_event)
        return resolved_output, emitted_events

    @staticmethod
    def check_pending_status(tool_output: str | dict[str, Any]) -> dict[str, Any] | None:
        try:
            data = json.loads(tool_output) if isinstance(tool_output, str) else tool_output
        except (json.JSONDecodeError, TypeError):
            return None

        if not isinstance(data, dict):
            return None

        status = data.get("status")
        if status == "pending_approval":
            return {
                "event": "approval_required",
                "data": {
                    "ticket_id": data.get("ticket_id", ""),
                    "action": data.get("action", ""),
                    "reason": data.get("reason", ""),
                },
            }
        if status == "pending_access_grant":
            return {
                "event": "access_grant_required",
                "data": {
                    "ticket_id": data.get("ticket_id", ""),
                    "target_type": data.get("target_type", ""),
                    "target_identifier": data.get("target_identifier", ""),
                    "display_name": data.get("display_name", ""),
                    "action_context": data.get("action_context", ""),
                },
            }
        return None

    async def await_ticket_resolution(
        self,
        *,
        budget: IterationBudget,
        ticket_id: str,
        event_kind: str,
        tool_dict: dict[str, Any],
        arguments: dict[str, Any],
        execute_tool: ToolExecute,
        timeout: float | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        if event_kind == "access_grant_required":
            event = register_grant_resume(ticket_id)
            get_decision = lambda: get_grant_resume_decision(ticket_id)
            cleanup = lambda: cleanup_grant_resume(ticket_id)
        else:
            event = register_approval_resume(ticket_id)
            get_decision = lambda: get_approval_resume_decision(ticket_id)
            cleanup = lambda: cleanup_approval_resume(ticket_id)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + (self.default_timeout_seconds if timeout is None else timeout)

        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    yield {
                        "_result": json.dumps(
                            {
                                "status": "error",
                                "error": "Permission request timed out after 10 minutes.",
                            }
                        )
                    }
                    return

                try:
                    await asyncio.wait_for(asyncio.shield(event.wait()), timeout=min(15.0, remaining))
                    break
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": {}}

            decision = get_decision()
            if decision in (None, "deny", "rejected"):
                yield {
                    "_result": json.dumps(
                        {
                            "status": "denied",
                            "reason": "User denied access. Cannot proceed with this action.",
                        }
                    )
                }
                return

            try:
                if event_kind == "approval_required":
                    resumed_output = await self._resume_approved_ticket(ticket_id)
                else:
                    resumed_output = await self._resume_access_grant(
                        ticket_id=ticket_id,
                        budget=budget,
                        tool_dict=tool_dict,
                        arguments=arguments,
                        execute_tool=execute_tool,
                    )
            except Exception as exc:
                resumed_output = json.dumps({"status": "error", "error": str(exc)})

            yield {
                "event": "tool_resumed",
                "data": {"tool": tool_dict.get("name", ""), "output": resumed_output},
            }
            yield {"_result": resumed_output}
        finally:
            cleanup()

    async def _resume_approved_ticket(self, ticket_id: str) -> str:
        ticket = get_approval_ticket(ticket_id)
        if ticket is None:
            return json.dumps(
                {"status": "error", "error": f"Approval ticket {ticket_id} not found after approval."}
            )

        updated_ticket = await asyncio.to_thread(resume_approved_ticket, ticket)
        return updated_ticket.execution_result or json.dumps({"status": "ok", "ticket_id": ticket_id})

    async def _resume_access_grant(
        self,
        *,
        ticket_id: str,
        budget: IterationBudget,
        tool_dict: dict[str, Any],
        arguments: dict[str, Any],
        execute_tool: ToolExecute,
    ) -> str:
        grant_ticket = get_grant_ticket(ticket_id)
        validation_error = (
            "ticket not found"
            if grant_ticket is None
            else grant_validation_error(
                grant_ticket,
                expected_session_id=current_control_session_id(),
                expected_conversation_id=current_conversation_id(),
                expected_execution_source=current_execution_source(),
                require_granted=True,
            )
        )
        if validation_error:
            return json.dumps({"status": "error", "error": f"Access grant invalidated: {validation_error}"})

        return await execute_tool(budget, tool_dict, dict(arguments))
