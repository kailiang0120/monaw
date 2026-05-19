import asyncio
import json

from app.agent.harness.tool_executor import ToolExecutor
from app.agent.iteration_budget import IterationBudget


def test_tool_executor_preserves_exception_reason_code():
    class ReasonedError(RuntimeError):
        reason_code = "managed_chrome_exited"

    async def failing_tool():
        raise ReasonedError("Chrome exited")

    executor = ToolExecutor()
    try:
        result = asyncio.run(
            executor.execute(
                tool_dict={"name": "browser_open", "callable": failing_tool, "execution_mode": "async"},
                arguments={},
                budget=IterationBudget(),
                call_id="call-1",
            )
        )
    finally:
        executor.shutdown()

    payload = json.loads(result.output)
    assert result.status == "error"
    assert payload["reason_code"] == "managed_chrome_exited"
    assert payload["tool"] == "browser_open"
    assert payload["error"] == "Chrome exited"
