from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import yaml

from app.agent.approval_broker import create_ticket
from app.agent.execution_resume import register_executor
from app.agent.run_context import current_control_session_id, current_conversation_id, current_execution_source
from app.agent.run_context import current_interactive
from app.agent.runtime_paths import RUNTIME_DIR

SKILLS_DIR = Path(__file__).resolve().parents[1]
SKILL_STAGING_DIR = RUNTIME_DIR / "skill_staging"
SKILL_ROLLBACK_DIR = RUNTIME_DIR / "skill_rollbacks"
RESTART_EXIT_CODE = 78
MAX_SKILL_FILES = 2
MAX_SKILL_BYTES = 256 * 1024
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
_RESERVED_WINDOWS_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _error(message: str, *, reason_code: str = "skill_creator_error", **extra: Any) -> str:
    return _json({"status": "error", "error": message, "reason_code": reason_code, **extra})


def _normalize_skill_name(value: str) -> tuple[str, str]:
    raw = str(value or "").strip().lower()
    if ":" in raw:
        raise ValueError("Skill name cannot contain alternate data stream syntax.")
    raw = raw.replace("_", "-")
    raw = re.sub(r"[^a-z0-9-]+", "-", raw)
    raw = re.sub(r"-{2,}", "-", raw).strip("-")
    if not raw:
        raise ValueError("Skill name is required.")
    if len(raw) > 64:
        raise ValueError("Skill name must be 64 characters or less.")
    if raw in _RESERVED_WINDOWS_NAMES:
        raise ValueError(f"'{raw}' is a reserved Windows name.")
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


def _safe_staging_dir(folder: str) -> Path:
    path = (SKILL_STAGING_DIR / f"{folder}-{int(time.time() * 1000)}").resolve()
    root = SKILL_STAGING_DIR.resolve()
    if root != path and root not in path.parents:
        raise ValueError("Skill staging path escapes the staging directory.")
    return path


def _safe_rollback_dir(folder: str) -> Path:
    path = (SKILL_ROLLBACK_DIR / f"{folder}-{int(time.time() * 1000)}").resolve()
    root = SKILL_ROLLBACK_DIR.resolve()
    if root != path and root not in path.parents:
        raise ValueError("Skill rollback path escapes the rollback directory.")
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


def _content_hash(files: dict[str, str]) -> str:
    import hashlib

    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[name].encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _validate_generated_files(files: dict[str, str]) -> None:
    if len(files) > MAX_SKILL_FILES:
        raise ValueError("Generated skill has too many files.")
    total_bytes = sum(len(value.encode("utf-8")) for value in files.values())
    if total_bytes > MAX_SKILL_BYTES:
        raise ValueError("Generated skill exceeds the maximum byte size.")
    for name in files:
        if name not in {"SKILL.md", "tools.py"}:
            raise ValueError(f"Generated skill contains unsupported file '{name}'.")
        if ":" in name or "/" in name or "\\" in name or name.startswith("."):
            raise ValueError(f"Generated skill contains unsafe file name '{name}'.")


def _raw_skill_create(
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
    skill_md_source = _skill_markdown(
        skill_name=skill_name,
        description=description,
        instructions=str(instructions or ""),
        enabled_by_default=False,
    )
    files = {"SKILL.md": skill_md_source}
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
        files["tools.py"] = tools_source.rstrip() + "\n"

    try:
        _validate_generated_files(files)
    except ValueError as exc:
        return _error(str(exc), reason_code="invalid_generated_skill")

    staging_dir: Path | None = None
    rollback_dir: Path | None = None
    try:
        SKILL_STAGING_DIR.mkdir(parents=True, exist_ok=True)
        SKILL_ROLLBACK_DIR.mkdir(parents=True, exist_ok=True)
        staging_dir = _safe_staging_dir(folder)
        staging_dir.mkdir(parents=True, exist_ok=False)
        for filename, content in files.items():
            target = staging_dir / filename
            target.write_text(content, encoding="utf-8")
            if target.is_symlink():
                raise ValueError("Generated skill cannot contain symlinks.")

        if skill_dir.exists():
            if not overwrite:
                return _error(
                    f"Skill '{skill_name}' already exists.",
                    reason_code="skill_exists",
                    skill_name=skill_name,
                    path=str(skill_dir),
                )
            rollback_dir = _safe_rollback_dir(folder)
            shutil.move(str(skill_dir), str(rollback_dir))
        skill_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging_dir), str(skill_dir))
        staging_dir = None

        # New executable skill code is never enabled or loaded in the same turn
        # that created it. A later settings change and runtime reload is required.
        _enable_skill(skill_name, False)
    except Exception as exc:
        if rollback_dir is not None and rollback_dir.exists() and not skill_dir.exists():
            shutil.move(str(rollback_dir), str(skill_dir))
        if staging_dir is not None and staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        return _error(str(exc), reason_code="write_failed", skill_name=skill_name, path=str(skill_dir))

    content_hash = _content_hash(files)
    return _json(
        {
            "status": "ok",
            "skill_name": skill_name,
            "folder": folder,
            "path": str(skill_dir),
            "skill_md": str(skill_md),
            "tools_py": str(tools_path) if tools_source else "",
            "enabled": False,
            "runtime_reloaded": False,
            "rollback_path": str(rollback_dir) if rollback_dir else "",
            "content_hash": content_hash,
            "author_session": current_control_session_id(),
            "source_conversation": current_conversation_id(),
            "execution_source": current_execution_source(),
            "activation_time": int(time.time()),
            "next_step": "Review the generated files, enable the skill in settings, then reload runtime in a later turn.",
        }
    )


def skill_create(
    name: str,
    description: str,
    instructions: str,
    tools_py: str = "",
    enabled: bool = False,
    overwrite: bool = False,
    reload_runtime: bool = True,
) -> str:
    if not current_interactive():
        return _error(
            "Skill creation requires an interactive administrator session.",
            reason_code="interactive_admin_required",
        )
    args = {
        "name": name,
        "description": description,
        "instructions": instructions,
        "tools_py": tools_py,
        "enabled": False,
        "overwrite": overwrite,
        "reload_runtime": False,
    }
    ticket = create_ticket(
        action_type="skill_creator",
        tool_name="skill_create",
        target_app="skill-creator",
        risk_level="high",
        reason="administrator_approval_required",
        action_description=f"Create or overwrite runtime skill {name}",
        payload={"input_str": json.dumps(args, ensure_ascii=False, sort_keys=True), "args": args},
    )
    return _json(
        {
            "status": "pending_approval",
            "ticket_id": ticket.id,
            "action": f"Create or overwrite runtime skill {name}",
            "reason": "administrator_approval_required",
            "next_step": "Approve only from a trusted local administrator session.",
        }
    )


def skill_reload() -> str:
    if not current_interactive():
        return _error(
            "Skill reload requires an interactive administrator session.",
            reason_code="interactive_admin_required",
        )
    ticket = create_ticket(
        action_type="skill_creator",
        tool_name="skill_reload",
        target_app="skill-creator",
        risk_level="medium",
        reason="administrator_approval_required",
        action_description="Reload runtime skill registry",
        payload={"input_str": "{}", "args": {}},
    )
    return _json({"status": "pending_approval", "ticket_id": ticket.id, "reason": "administrator_approval_required"})


def _raw_skill_reload() -> str:
    try:
        _reload_runtime()
    except Exception as exc:
        return _error(str(exc), reason_code="reload_failed")
    return _json({"status": "ok", "runtime_reloaded": True})


def _schedule_backend_restart(delay_seconds: int = 3, max_wait_seconds: int = 90) -> str:
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


def backend_restart(delay_seconds: int = 3, max_wait_seconds: int = 90) -> str:
    if not current_interactive():
        return _error(
            "Backend restart requires an interactive administrator session.",
            reason_code="interactive_admin_required",
        )
    args = {
        "delay_seconds": max(1, min(30, int(delay_seconds or 3))),
        "max_wait_seconds": max(3, min(300, int(max_wait_seconds or 90))),
    }
    ticket = create_ticket(
        action_type="skill_creator",
        tool_name="backend_restart",
        target_app="skill-creator",
        risk_level="high",
        reason="administrator_approval_required",
        action_description="Restart Python backend process",
        payload={"input_str": json.dumps(args, ensure_ascii=False, sort_keys=True), "args": args},
    )
    return _json({"status": "pending_approval", "ticket_id": ticket.id, "reason": "administrator_approval_required"})


def _raw_backend_restart(delay_seconds: int = 3, max_wait_seconds: int = 90) -> str:
    return _schedule_backend_restart(delay_seconds=delay_seconds, max_wait_seconds=max_wait_seconds)


def skill_list(settings) -> str:
    try:
        from app.agent.skill_loader import available_skill_payload

        skills = available_skill_payload(settings)
    except Exception as exc:
        return _error(str(exc), reason_code="list_failed")
    return _json({"status": "ok", "skills": skills})


def register_tools(registry, settings) -> None:
    register_executor(
        "skill_create",
        lambda input_str: _raw_skill_create(**(json.loads(input_str) if input_str else {})),
    )
    register_executor("skill_reload", lambda _input_str: _raw_skill_reload())
    register_executor(
        "backend_restart",
        lambda input_str: _raw_backend_restart(**(json.loads(input_str) if input_str else {})),
    )

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
                    "risk_level": "high",
                    "required_scope": "administrator",
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
                    "risk_level": "medium",
                    "required_scope": "administrator",
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
                    "required_scope": "administrator",
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
