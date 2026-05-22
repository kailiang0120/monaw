from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import yaml

SKILLS_DIR = Path(__file__).resolve().parents[1]
RESTART_EXIT_CODE = 78
_PROTECTED_SKILL_NAMES = {
    "core",
    "exec",
    "computer-use",
    "filesystem",
    "memory",
    "skill-creator",
    "browser-use",
    "scheduling",
    "mcp-bridge",
}


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _error(message: str, *, reason_code: str = "skill_creator_error", **extra: Any) -> str:
    return _json({"status": "error", "error": message, "reason_code": reason_code, **extra})


def _normalize_skill_name(value: str) -> tuple[str, str]:
    raw = str(value or "").strip().lower()
    raw = raw.replace("_", "-")
    raw = re.sub(r"[^a-z0-9-]+", "-", raw)
    raw = re.sub(r"-{2,}", "-", raw).strip("-")
    if not raw:
        raise ValueError("Skill name is required.")
    if len(raw) > 64:
        raise ValueError("Skill name must be 64 characters or less.")
    if raw in _PROTECTED_SKILL_NAMES:
        raise ValueError(f"'{raw}' is a protected built-in skill name.")
    folder = raw.replace("-", "_")
    return raw, folder


def _safe_skill_dir(folder: str) -> Path:
    path = (SKILLS_DIR / folder).resolve()
    root = SKILLS_DIR.resolve()
    if root != path and root not in path.parents:
        raise ValueError("Skill path escapes the skills directory.")
    return path


def _skill_markdown(
    *,
    skill_name: str,
    description: str,
    instructions: str,
    enabled_by_default: bool,
) -> str:
    frontmatter = {
        "name": skill_name,
        "description": description.strip(),
        "version": "1.0.0",
        "enabled_by_default": bool(enabled_by_default),
        "tier": "optional",
    }
    body = instructions.strip() or (
        f"# {skill_name}\n\n"
        "Use this skill for the workflow described in the frontmatter. Keep actions scoped, "
        "verify results with the smallest practical check, and return concise status updates."
    )
    return "---\n" + yaml.safe_dump(frontmatter, sort_keys=False).strip() + "\n---\n\n" + body + "\n"


def _enable_skill(skill_name: str, enabled: bool) -> None:
    from app.agent.settings_store import load_agent_settings, save_agent_settings

    settings_data = load_agent_settings()
    settings_data.tools.skills[skill_name] = bool(enabled)
    save_agent_settings(settings_data)


def _reload_runtime() -> None:
    from app.agent.runtime import reset_runtime_cache

    reset_runtime_cache(reset_mcp=False, reset_browser=False)


def skill_create(
    name: str,
    description: str,
    instructions: str,
    tools_py: str = "",
    enabled: bool = False,
    overwrite: bool = False,
    reload_runtime: bool = True,
) -> str:
    try:
        skill_name, folder = _normalize_skill_name(name)
        skill_dir = _safe_skill_dir(folder)
    except ValueError as exc:
        return _error(str(exc), reason_code="invalid_skill_name")

    description = str(description or "").strip()
    if not description:
        return _error("Skill description is required.", reason_code="missing_description")

    skill_md = skill_dir / "SKILL.md"
    tools_path = skill_dir / "tools.py"
    if skill_dir.exists() and not overwrite:
        return _error(
            f"Skill '{skill_name}' already exists.",
            reason_code="skill_exists",
            skill_name=skill_name,
            path=str(skill_dir),
        )

    tools_source = str(tools_py or "").strip()
    if tools_source:
        try:
            compile(tools_source, str(tools_path), "exec")
        except SyntaxError as exc:
            return _error(
                f"tools.py has invalid Python syntax: {exc.msg}",
                reason_code="invalid_tools_py",
                line=exc.lineno,
                offset=exc.offset,
            )

    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_md.write_text(
            _skill_markdown(
                skill_name=skill_name,
                description=description,
                instructions=str(instructions or ""),
                enabled_by_default=False,
            ),
            encoding="utf-8",
        )
        if tools_source:
            tools_path.write_text(tools_source.rstrip() + "\n", encoding="utf-8")
        elif overwrite and tools_path.exists():
            tools_path.unlink()

        _enable_skill(skill_name, bool(enabled))
        if reload_runtime:
            _reload_runtime()
    except Exception as exc:
        return _error(str(exc), reason_code="write_failed", skill_name=skill_name, path=str(skill_dir))

    return _json(
        {
            "status": "ok",
            "skill_name": skill_name,
            "folder": folder,
            "path": str(skill_dir),
            "skill_md": str(skill_md),
            "tools_py": str(tools_path) if tools_source else "",
            "enabled": bool(enabled),
            "runtime_reloaded": bool(reload_runtime),
            "next_step": "Use the new skill after it is enabled, or call skill_reload after manual edits.",
        }
    )


def skill_reload() -> str:
    try:
        _reload_runtime()
    except Exception as exc:
        return _error(str(exc), reason_code="reload_failed")
    return _json({"status": "ok", "runtime_reloaded": True})


def backend_restart(delay_seconds: int = 3, max_wait_seconds: int = 90) -> str:
    delay = max(1, min(30, int(delay_seconds or 3)))
    max_wait = max(delay, min(300, int(max_wait_seconds or 90)))

    def _exit_process() -> None:
        deadline = time.monotonic() + max_wait
        time.sleep(delay)
        while time.monotonic() < deadline:
            try:
                from app.agent.runtime import active_runtime_run_count

                if active_runtime_run_count() <= 0:
                    break
            except Exception:
                break
            time.sleep(1)
        os._exit(RESTART_EXIT_CODE)

    timer = threading.Timer(delay, _exit_process)
    timer.daemon = True
    timer.start()
    return _json(
        {
            "status": "ok",
            "restart_scheduled": True,
            "delay_seconds": delay,
            "max_wait_seconds": max_wait,
            "exit_code": RESTART_EXIT_CODE,
            "note": "The backend will restart after active agent runs become idle, and Electron will respawn it in the desktop app.",
        }
    )


def skill_list(settings) -> str:
    try:
        from app.agent.skill_loader import available_skill_payload

        skills = available_skill_payload(settings)
    except Exception as exc:
        return _error(str(exc), reason_code="list_failed")
    return _json({"status": "ok", "skills": skills})


def register_tools(registry, settings) -> None:
    registry.extend(
        [
            {
                "name": "skill_create",
                "description": (
                    "Create or overwrite an optional Monaw runtime skill under backend/app/skills. "
                    "Use for new SKILL.md instructions and optional tools.py code."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Skill name, normalized to hyphen-case."},
                        "description": {"type": "string", "description": "Trigger description for the skill frontmatter."},
                        "instructions": {"type": "string", "description": "Markdown body for SKILL.md."},
                        "tools_py": {"type": "string", "default": "", "description": "Optional complete Python tools.py source."},
                        "enabled": {"type": "boolean", "default": False, "description": "Enable the new skill immediately."},
                        "overwrite": {"type": "boolean", "default": False, "description": "Replace an existing non-built-in skill."},
                        "reload_runtime": {"type": "boolean", "default": True, "description": "Reload runtime tools after writing."},
                    },
                    "required": ["name", "description", "instructions"],
                },
                "callable": skill_create,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": {
                    "parallel_safe": False,
                    "resource_locks": ["skills_dir", "settings", "runtime_cache"],
                    "mutates_state": True,
                    "risk_level": "medium",
                    "search_tags": ["skills", "create skill", "tools.py", "reload runtime"],
                },
            },
            {
                "name": "skill_reload",
                "description": "Reload the Monaw runtime cache so newly edited skills and tools are discovered.",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": skill_reload,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": {
                    "parallel_safe": False,
                    "resource_locks": ["runtime_cache"],
                    "mutates_state": True,
                    "risk_level": "low",
                    "search_tags": ["reload skills", "runtime cache"],
                },
            },
            {
                "name": "backend_restart",
                "description": (
                    "Restart the Python backend after a skill or dependency update. "
                    "Use only when runtime reload is insufficient."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "delay_seconds": {"type": "integer", "default": 3, "description": "Minimum delay before restart, 1 to 30 seconds."},
                        "max_wait_seconds": {"type": "integer", "default": 90, "description": "Maximum wait for active runs to finish, 3 to 300 seconds."},
                    },
                    "required": [],
                },
                "callable": backend_restart,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": {
                    "parallel_safe": False,
                    "resource_locks": ["backend_process"],
                    "mutates_state": True,
                    "risk_level": "high",
                    "search_tags": ["restart backend", "reload server"],
                },
            },
            {
                "name": "skill_list",
                "description": "List discovered Monaw runtime skills with enabled and availability status.",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": lambda: skill_list(settings),
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": {
                    "parallel_safe": True,
                    "resource_locks": [],
                    "mutates_state": False,
                    "risk_level": "low",
                    "search_tags": ["skills", "list skills", "available skills"],
                },
            },
        ]
    )
