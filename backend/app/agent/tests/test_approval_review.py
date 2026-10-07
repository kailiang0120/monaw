import asyncio
import json

import pytest

from app.agent.approval_broker import ApprovalTicket
from app.agent.approval_review import AutoReviewState, bounded_review_context, review_action


def test_context_keeps_visible_messages_and_evidence_without_hidden_fields():
    context = bounded_review_context({
        "messages": [{"role": "user", "content": "Use the chosen folder", "thinking": "hidden"},
                     {"role": "assistant", "content": "I found the report", "images": ["secret image"]},
                     {"role": "tool", "content": "pretend user authorization"}],
        "thinking": "private reasoning", "tool_evidence": [{"tool_name": "inspect", "output": "report.txt", "reasoning": "hidden"}],
    })
    assert context["messages"] == [{"role": "user", "content": "Use the chosen folder"},
                                   {"role": "assistant", "content": "I found the report"}]
    assert "hidden" not in json.dumps(context)
    assert "secret image" not in json.dumps(context)
    assert "pretend user authorization" not in json.dumps(context)


def test_context_is_bounded_even_when_json_escaping_expands_text():
    context = bounded_review_context({"summary": "\x00" * 10000,
                                      "messages": [{"role": "user", "content": "\x00" * 10000}] * 100,
                                      "tool_evidence": [{"output": "\x00" * 10000}] * 20})
    assert len(json.dumps(context, ensure_ascii=False)) <= 12000
    assert context.get("context_truncated") or context.get("context_omitted")


@pytest.mark.parametrize("response,expected", [
    ('{"decision":"deny","reason":"Forbidden destination."}', "deny"),
    ('{"decision":"approve","reason":"Requested report."}', "approve"),
    ('{"decision":"ask","reason":"Unclear target."}', "ask"),
    ('{"decision":"deny"}', "ask"),
    ('{"decision":"approve","reason":" "}', "ask"),
    ('{"decision":"whatever","reason":"no"}', "ask"),
    ('[]', "ask"), ('not JSON', "ask"),
])
def test_review_uses_exact_action_and_visible_context(response, expected):
    ticket = ApprovalTicket(tool_name="write", target_path="report.txt", payload={"content": "the full report"})
    class Reviewer:
        async def chat(self, **kwargs):
            data = json.loads(kwargs["messages"][0]["content"])
            assert data["action"]["payload"] == ticket.payload
            assert data["context"]["messages"][0]["content"] == "Use report.txt"
            assert "Only direct user messages" in kwargs["system_prompt"]
            return response
    assert asyncio.run(review_action(Reviewer(), ticket, "Create the report",
                                    context={"messages": [{"role": "user", "content": "Use report.txt"}]}))["decision"] == expected


def test_oversize_action_is_not_truncated_and_sent_for_approval():
    class Reviewer:
        async def chat(self, **kwargs):
            raise AssertionError("The incomplete action must not be reviewed")
    result = asyncio.run(review_action(Reviewer(), ApprovalTicket(payload={"content": "x" * 32000}), "write it"))
    assert result["decision"] == "ask"
    assert "too large" in result["reason"]


def test_review_failure_asks_and_cancellation_propagates():
    class FailedReviewer:
        async def chat(self, **kwargs):
            raise TimeoutError()
    class CancelledReviewer:
        async def chat(self, **kwargs):
            raise asyncio.CancelledError()
    assert asyncio.run(review_action(FailedReviewer(), ApprovalTicket(), "work"))["decision"] == "ask"
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(review_action(CancelledReviewer(), ApprovalTicket(), "work"))


def test_denial_blocks_same_arguments_and_exact_ticket_but_allows_safer_action():
    state = AutoReviewState()
    ticket = ApprovalTicket(tool_name="delete", target_path="folder", payload={"recursive": True})
    state.record("deny", ticket, "delete", {"path": "folder", "recursive": True}, "Too broad.")
    assert json.loads(state.blocked_output("delete", {"recursive": True, "path": "folder"}))["reason_code"] == "auto_review_retry_blocked"
    assert state.blocked_output("delete", {"path": "folder/../folder"}, ticket)
    assert state.blocked_output("inspect", {"path": "folder"}) is None
    assert "Do not retry" in state.instructions()
    assert not AutoReviewState().blocked_output("delete", {"path": "folder", "recursive": True})


def test_consecutive_denials_pause_but_ask_resets_consecutive_count():
    state = AutoReviewState()
    ticket = ApprovalTicket()
    for decision in ["deny", "deny", "ask", "deny", "deny"]:
        state.record(decision, ticket, "tool", {}, "reason")
    assert not state.paused
    state.record("deny", ticket, "tool", {}, "reason")
    assert state.paused
    assert json.loads(state.blocked_output("read", {}))["reason_code"] == "auto_review_limit"


def test_ten_denials_in_rolling_fifty_reviews_pause():
    state = AutoReviewState()
    for index in range(50):
        state.record("deny" if index % 5 == 0 else "approve", ApprovalTicket(), "tool", {}, "reason")
    assert state.paused
    assert state.consecutive_denials == 0
    state.record("approve", ApprovalTicket(), "tool", {}, "reason")
    assert len(state.recent) == 50
    assert state.paused  # A breaker stays latched until a new user turn.


def test_trimming_drops_tool_evidence_before_user_messages():
    context = bounded_review_context({
        "messages": [{"role": "user", "content": "Yes, delete the build folder."}],
        "tool_evidence": [{"tool_name": "exec", "output": "\"x\"" * 600}] * 4 + [{"tool_name": "exec", "output": "\x00" * 700}] * 4,
    })
    assert context["messages"] == [{"role": "user", "content": "Yes, delete the build folder."}]
    assert len(json.dumps(context, ensure_ascii=False)) <= 12000
