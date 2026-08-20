"""Shared execution artifact storage."""

from __future__ import annotations

from app.agent.runtime_paths import RUNTIME_DIR

EXEC_ARTIFACT_DIR = RUNTIME_DIR / "exec"
EXEC_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)


def write_artifact(command_id: str, stream_name: str, content: str) -> str:
    path = EXEC_ARTIFACT_DIR / f"{command_id}.{stream_name}.txt"
    path.write_text(content, encoding="utf-8", newline="")
    return str(path)

