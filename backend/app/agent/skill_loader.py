"""Discovery and loading for Monaw skills."""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import os
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.agent.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"
CURRENT_OS = "windows" if os.name == "nt" else "posix"
_LAST_LOAD_ERRORS: dict[str, str] = {}


@dataclass(slots=True, frozen=True)
class SkillCatalogEntry:
    slug: str
    name: str
    path: Path
    frontmatter: dict[str, Any]
    body: str


@dataclass(slots=True)
class SkillSpec:
    slug: str
    name: str
    description: str
    version: str
    body: str
    path: Path
    display_name: str = ""
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    enabled_by_default: bool = True
    always: bool = False
    enabled: bool = False
    available: bool = True
    unavailable_reason: str = ""
    load_error: str = ""
    tier: str = "recommended"
    recommended: bool = True


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    if not raw.startswith("---"):
        return {}, raw.strip()

    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw.strip()

    frontmatter = yaml.safe_load(parts[1]) or {}
    body = parts[2].strip()
    return frontmatter, body


def _normalize_skill_name(folder_name: str, frontmatter: dict[str, Any]) -> str:
    return str(frontmatter.get("name") or folder_name.replace("_", "-")).strip()


def _skill_catalog(*, skills_dir: Path | None = None) -> list[SkillCatalogEntry]:
    base_dir = skills_dir or SKILLS_DIR
    if not base_dir.exists():
        return []

    catalog: list[SkillCatalogEntry] = []
    for skill_dir in sorted(path for path in base_dir.iterdir() if path.is_dir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            frontmatter, body = _split_frontmatter(skill_md.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("skill=%s metadata read failed: %s", skill_dir.name, exc)
            continue
        catalog.append(
            SkillCatalogEntry(
                slug=skill_dir.name,
                name=_normalize_skill_name(skill_dir.name, frontmatter),
                path=skill_dir,
                frontmatter=frontmatter,
                body=body,
            )
        )
    return catalog


def discovered_skill_names(*, skills_dir: Path | None = None) -> set[str]:
    """Return canonical names for all local skills with valid SKILL.md files."""

    return {entry.name for entry in _skill_catalog(skills_dir=skills_dir)}


def default_skill_flags(*, skills_dir: Path | None = None) -> dict[str, bool]:
    """Return the persisted defaults derived from each skill's frontmatter."""

    return {
        entry.name: bool(entry.frontmatter.get("always", False))
        or bool(entry.frontmatter.get("enabled_by_default", True))
        for entry in _skill_catalog(skills_dir=skills_dir)
    }


def _os_allowed(frontmatter: dict[str, Any]) -> bool:
    allowed = frontmatter.get("os")
    if not allowed:
        return True
    if isinstance(allowed, str):
        allowed = [allowed]
    values = {str(value).lower() for value in allowed}
    return CURRENT_OS in values


def _env_allowed(frontmatter: dict[str, Any], settings) -> tuple[bool, str]:
    """Return (allowed, reason) — reason is empty string when allowed."""
    requires_meta = (
        frontmatter.get("metadata", {})
        .get("openclaw", {})
        .get("requires", {})
    )
    requires = requires_meta.get("env", [])
    if isinstance(requires, str):
        requires = [requires]

    for env_name in requires or []:
        attr_name = str(env_name).lower()
        if os.getenv(str(env_name)):
            continue
        if getattr(settings, attr_name, ""):
            continue
        return False, f"missing env var '{env_name}'"

    python_modules = requires_meta.get("python_module", [])
    if isinstance(python_modules, str):
        python_modules = [python_modules]

    for module_name in python_modules or []:
        if importlib.util.find_spec(str(module_name)) is None:
            return False, f"missing python module '{module_name}'"

    return True, ""


def _skill_toggle_enabled(frontmatter: dict[str, Any], settings, skill_name: str) -> bool:
    if skill_name == "mcp-bridge" and not bool(
        getattr(getattr(settings, "mcp", object()), "enabled", True)
    ):
        return False
    if frontmatter.get("always") is True:
        return True

    enabled_by_default = bool(frontmatter.get("enabled_by_default", True))
    skills_map = getattr(getattr(settings, "tools", object()), "skills", {}) or {}
    return bool(skills_map.get(skill_name, enabled_by_default))


def _skill_tier(frontmatter: dict[str, Any]) -> str:
    value = str(frontmatter.get("tier", "recommended")).strip().lower()
    if value not in {"internal", "recommended", "optional"}:
        return "recommended"
    return value


def _load_skill_module(skill_dir: Path):
    module_path = skill_dir / "tools.py"
    if not module_path.exists():
        return None

    pkg_name = f"app.skills.{skill_dir.name}"
    module_name = f"{pkg_name}.tools"

    # Register a package entry so relative imports inside tools.py resolve correctly
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(skill_dir)]  # type: ignore[attr-defined]
        pkg.__package__ = pkg_name
        sys.modules[pkg_name] = pkg

    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to import tools from {module_path}")

    module = importlib.util.module_from_spec(spec)
    module.__package__ = pkg_name
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def discover_skills(
    settings,
    *,
    skills_dir: Path | None = None,
    include_disabled: bool = True,
) -> list[SkillSpec]:
    discovered: list[SkillSpec] = []
    for entry in _skill_catalog(skills_dir=skills_dir):
        frontmatter = entry.frontmatter
        body = entry.body
        name = entry.name

        env_ok, env_reason = _env_allowed(frontmatter, settings)
        os_ok = _os_allowed(frontmatter)
        available = os_ok and env_ok
        enabled = available and _skill_toggle_enabled(frontmatter, settings, name)

        # L2: Log why a skill is unavailable so operators don't have to guess.
        unavailable_reason = ""
        if not os_ok:
            unavailable_reason = "unsupported_os"
            logger.info("skill=%s disabled: unsupported OS (%s)", name, CURRENT_OS)
        elif not env_ok:
            unavailable_reason = "missing_env"
            logger.info("skill=%s disabled: %s", name, env_reason)

        spec = SkillSpec(
            slug=entry.slug,
            name=name,
            description=str(frontmatter.get("description", "")).strip(),
            display_name=str(frontmatter.get("display_name") or name).strip(),
            summary=str(frontmatter.get("summary") or frontmatter.get("description", "")).strip(),
            version=str(frontmatter.get("version", "1.0.0")).strip(),
            body=body,
            path=entry.path,
            metadata=frontmatter.get("metadata", {}) or {},
            enabled_by_default=bool(frontmatter.get("enabled_by_default", True)),
            always=bool(frontmatter.get("always", False)),
            enabled=enabled,
            available=available,
            unavailable_reason=unavailable_reason,
            load_error=_LAST_LOAD_ERRORS.get(name, ""),
            tier=_skill_tier(frontmatter),
            recommended=_skill_tier(frontmatter) == "recommended",
        )
        if include_disabled or spec.enabled:
            discovered.append(spec)
    return discovered


def _register_tool_search(registry: ToolRegistry) -> None:
    def tool_search(query: str, limit: int = 8, activate: bool = True) -> str:
        matches = registry.search_tools(
            query,
            limit=max(1, min(20, int(limit or 8))),
            include_hidden=True,
        )
        activate_names = [
            str(match.get("name") or "")
            for match in matches
            if not bool(match.get("visible", True))
            and bool(match.get("dynamic_load", False))
        ]
        activated = registry.set_visibility(activate_names, True) if activate else []
        if activated:
            logger.info(
                "tool_search activated %d tools for query=%r: %s",
                len(activated),
                query,
                ", ".join(activated),
            )
        return json.dumps(
            {
                "status": "ok",
                "query": query,
                "activated": activated,
                "matches": matches,
                "next_step": (
                    "Call one of the activated tools with the user's requested arguments."
                    if activated
                    else "Use a matching visible tool, or answer directly if no tool is needed."
                ),
            },
            ensure_ascii=False,
        )

    registry.register(
        {
            "name": "tool_search",
            "description": (
                "Search hidden or specialized tools by capability and make matching tools visible "
                "for the next model step. Use this when the capability checklist says a feature "
                "exists but no matching tool schema is currently visible."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Short capability query, e.g. 'scheduled task cron'."},
                    "limit": {"type": "integer", "default": 8},
                    "activate": {"type": "boolean", "default": True},
                },
                "required": ["query"],
            },
            "callable": tool_search,
            "domain": "general",
            "execution_mode": "sync_stateless",
            "affinity_group": None,
            "metadata": {
                "parallel_safe": False,
                "resource_locks": ["tool_registry"],
                "mutates_state": True,
                "risk_level": "low",
                "repeat_safe": True,
            },
        }
    )


def _invoke_register_tools(register_tools, registry: ToolRegistry, settings) -> None:
    signature = inspect.signature(register_tools)
    if len(signature.parameters) >= 2:
        register_tools(registry, settings)
    else:
        register_tools(registry)


def load_tools(settings, *, skills_dir: Path | None = None) -> tuple[list[SkillSpec], ToolRegistry]:
    skill_specs = discover_skills(settings, skills_dir=skills_dir, include_disabled=False)
    registry = ToolRegistry()

    for skill in skill_specs:
        try:
            module = _load_skill_module(skill.path)
            if module is None:
                continue

            register_tools = getattr(module, "register_tools", None)
            if register_tools is None:
                continue

            staged_registry = ToolRegistry()
            _invoke_register_tools(register_tools, staged_registry, settings)
            registry.extend(staged_registry.get_all_tools())
            _LAST_LOAD_ERRORS.pop(skill.name, None)
        except Exception as exc:
            message = str(exc)
            _LAST_LOAD_ERRORS[skill.name] = message
            skill.available = False
            skill.enabled = False
            skill.unavailable_reason = "load_error"
            skill.load_error = message
            logger.exception("skill=%s load failed: %s", skill.name, exc)

    _register_tool_search(registry)
    return skill_specs, registry


def available_skill_payload(settings) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for skill in discover_skills(settings, include_disabled=True):
        if skill.tier == "internal":
            continue
        load_error = skill.load_error or _LAST_LOAD_ERRORS.get(skill.name, "")
        payload.append(
            {
                "slug": skill.slug,
                "name": skill.name,
                "display_name": skill.display_name,
                "summary": skill.summary,
                "description": skill.description,
                "version": skill.version,
                "enabled_by_default": skill.enabled_by_default,
                "enabled": skill.enabled and not load_error,
                "available": skill.available and not load_error,
                "always": skill.always,
                "unavailable_reason": "load_error" if load_error else skill.unavailable_reason,
                "load_error": load_error,
                "tier": skill.tier,
                "recommended": skill.recommended,
            }
        )
    return payload
