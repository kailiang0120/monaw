"""Prompt template loading helpers."""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
from pathlib import PurePath


_PROMPT_PACKAGE = "app.agent.prompts"


@lru_cache(maxsize=32)
def load_prompt_template(name: str) -> str:
    prompt_name = str(name or "").strip()
    if not prompt_name or PurePath(prompt_name).name != prompt_name:
        raise ValueError(f"Invalid prompt template name: {name!r}")

    try:
        text = resources.files(_PROMPT_PACKAGE).joinpath(prompt_name).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RuntimeError(f"Prompt template not found: {prompt_name}") from exc

    return text.strip()
