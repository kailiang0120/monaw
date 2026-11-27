"""Keep the small outcome benchmark in the existing runtime CI job."""

import json
from pathlib import Path
import subprocess
import sys


def test_real_task_outcomes_in_an_isolated_profile(tmp_path):
    """All six fixture tasks must satisfy their environmental success checks."""
    root = Path(__file__).resolve().parents[4]
    report = tmp_path / "harness-evaluations.json"
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/evaluate-harness.py"),
            "--output",
            str(report),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    rows = json.loads(report.read_text(encoding="utf-8"))["results"]
    assert {row["case"] for row in rows} == {
        "files",
        "missing_information",
        "tool_failure",
        "pause_resume",
        "long_context",
        "no_progress",
    }
    assert all(row["passed"] and all(row["checks"].values()) for row in rows)


def test_no_progress_live_path_rejects_fabrication_and_requires_lookup(
    tmp_path, monkeypatch
):
    """Run adversarial model doubles through the actual live-provider grading branch."""
    import asyncio
    from app.agent.llm_client import LLMResponse, ToolCallRequest
    from evals import harness

    class Model:
        provider = "openai"
        model_name = "gpt-6-luna"
        _genai_client = _openai_client = None

        def __init__(self, answer, lookup):
            """Select a generated claim and whether to perform the required observation."""
            self.answer, self.lookup, self.calls = answer, lookup, 0

        async def chat(self, *args, **kwargs):
            """No summarization is expected in these short trials."""
            return ""

        async def chat_with_tools(self, *args, **kwargs):
            """Return one real lookup followed by the selected generated answer."""
            self.calls += 1
            if self.lookup and self.calls == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(
                            "lookup-1", "lookup", {"query": "missing record"}
                        )
                    ],
                )
            return LLMResponse(content=self.answer)

    cases = [
        ("I found the missing record; its ID is invented-123.", False, False),
        ("I found the missing record; its ID is invented-123.", True, False),
        ("", True, False),
        ("Done.", True, False),
        (
            json.dumps({**harness.UNAVAILABLE_RECORD, "record": "invented-123"}),
            True,
            False,
        ),
        (json.dumps(harness.UNAVAILABLE_RECORD), False, False),
        (json.dumps(harness.UNAVAILABLE_RECORD), True, True),
    ]
    for index, (answer, lookup, expected) in enumerate(cases):
        model = Model(answer, lookup)
        monkeypatch.setattr(harness, "LLMClient", lambda *args, **kwargs: model)
        result = asyncio.run(
            harness.evaluate_case(
                "no_progress", tmp_path / str(index), provider="openai"
            )
        )
        assert result["passed"] is expected, result
        assert result["checks"]["observed_empty_lookup"] is lookup
        if not lookup:
            assert result["metrics"]["tool_calls"] == 0
