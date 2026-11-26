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
        [sys.executable, str(root / "scripts/evaluate-harness.py"), "--output", str(report)],
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
