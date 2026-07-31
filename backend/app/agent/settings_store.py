"""Canonical runtime settings store for the agent."""

from __future__ import annotations

import json
import os
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from app.agent.llm_constants import (
    DEFAULT_GEMINI_CHAT_MODEL,
    DEFAULT_VISION_FALLBACK_MAX_OUTPUT_TOKENS,
    DEFAULT_VISION_FALLBACK_MODEL,
    GEMINI_CHAT_MODELS,
    VISION_FALLBACK_MODELS,
)
from app.agent.identity import DEFAULT_AGENT_NAME, LEGACY_AGENT_NAME
from app.agent.output_workspace import default_downloads_dir, default_screenshots_dir
from app.agent.runtime_paths import MONAW_HOME_DIR, RUNTIME_DIR

_USER_HOME = Path(os.path.expanduser("~"))
_RUNTIME_DIR = RUNTIME_DIR
_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
_SETTINGS_FILE = _RUNTIME_DIR / "settings.json"

DEFAULT_BLOCKED_ROOTS = [
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
    r"C:\$Recycle.Bin",
    str(_USER_HOME / "AppData"),
]

DEFAULT_FOLDER_RULES: list[str] = []

DEFAULT_SKILLS = {
    "core": True,
    "exec": True,
    "computer-use": True,
    "filesystem": True,
    "memory": True,
    "skill-creator": False,
    "background-check": False,
    "browser-use": True,
}
LEGACY_SKILL_ALIASES = {
    "desktop-control-win": "computer-use",
}
REMOVED_SKILLS = frozenset({"mcp-bridge"})
KNOWN_SKILLS = frozenset((*DEFAULT_SKILLS.keys(), *LEGACY_SKILL_ALIASES.keys(), "scheduling"))

_BROWSER_RUNTIME_DIR = MONAW_HOME_DIR / "browser"
_BROWSER_MANAGED_PROFILE_DIR = _BROWSER_RUNTIME_DIR / "profiles" / "managed"
_BROWSER_DOWNLOADS_DIR = default_downloads_dir()
_BROWSER_SCREENSHOTS_DIR = default_screenshots_dir()
_BROWSER_TRACES_DIR = _BROWSER_RUNTIME_DIR / "traces"
_LEGACY_BROWSER_RUNTIME_DIR = _RUNTIME_DIR / "browser-use"
_LEGACY_BROWSER_DOWNLOADS_DIR = _LEGACY_BROWSER_RUNTIME_DIR / "downloads"
_LEGACY_BROWSER_SCREENSHOTS_DIR = _LEGACY_BROWSER_RUNTIME_DIR / "screenshots"
_LEGACY_PUBLIC_OUTPUT_ROOT = (_USER_HOME / "Documents" if (_USER_HOME / "Documents").exists() else _USER_HOME) / "Monaw Agent"
_LEGACY_PUBLIC_DOWNLOADS_DIR = _LEGACY_PUBLIC_OUTPUT_ROOT / "Downloads"
_LEGACY_PUBLIC_SCREENSHOTS_DIR = _LEGACY_PUBLIC_OUTPUT_ROOT / "Screenshots"


def _default_browser_path(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _default_path_rules() -> list["PathRule"]:
    return [
        PathRule(path=path, read=True, write=True, delete=True, launch=False, enabled=True)
        for path in DEFAULT_FOLDER_RULES
    ]


class ConfirmationSettings(BaseModel):
    mutate: bool = True
    delete: bool = True
    launch_app: bool = True
    click: bool = True
    type: bool = True


class PathRule(BaseModel):
    path: str
    read: bool = True
    write: bool = True
    delete: bool = True
    launch: bool = False
    require_confirmation: bool = False
    enabled: bool = True


class AppRule(BaseModel):
    alias: str
    display_name: str = ""
    exe_paths: list[str] = Field(default_factory=list)
    launch_allowed: bool = True
    uia_allowed: bool = True
    screen_fallback_allowed: bool = False
    require_confirmation: bool = False
    enabled: bool = True


class PermissionProfileSettings(BaseModel):
    confirmations: ConfirmationSettings = Field(default_factory=ConfirmationSettings)
    blocked_roots: list[str] = Field(default_factory=lambda: list(DEFAULT_BLOCKED_ROOTS))
    path_rules: list[PathRule] = Field(default_factory=_default_path_rules)
    app_rules: list[AppRule] = Field(default_factory=list)
    allow_delete: bool = False
    dangerous_actions_require_confirm: bool = True
    allow_screen_fallback: bool = False


class LLMSettings(BaseModel):
    provider: Literal["openai", "deepseek", "gemini"] = "openai"
    model_name: str = "gpt-5.4"
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] = "medium"
    vision_fallback_enabled: bool = True
    vision_fallback_model: str = DEFAULT_VISION_FALLBACK_MODEL
    vision_fallback_max_output_tokens: int = Field(
        DEFAULT_VISION_FALLBACK_MAX_OUTPUT_TOKENS,
        ge=128,
        le=4000,
    )
    max_iterations_per_turn: int = Field(40, ge=1, le=500)
    max_turn_seconds: int = Field(1800, ge=30, le=14400)
    max_llm_call_seconds: int = Field(300, ge=30, le=1800)

    @model_validator(mode="after")
    def normalize_provider_models(self) -> "LLMSettings":
        if self.provider == "gemini" and self.model_name not in GEMINI_CHAT_MODELS:
            self.model_name = DEFAULT_GEMINI_CHAT_MODEL
        if self.vision_fallback_model not in VISION_FALLBACK_MODELS:
            self.vision_fallback_model = DEFAULT_VISION_FALLBACK_MODEL
        return self


class SpeechToTextSettings(BaseModel):
    engine: Literal["local", "cloud"] = "local"
    local_model: str = "base"
    cloud_provider: Literal["gemini"] = "gemini"
    cloud_model: str = "gemini-2.5-flash"

    @model_validator(mode="after")
    def normalize_values(self) -> "SpeechToTextSettings":
        if self.engine not in {"local", "cloud"}:
            self.engine = "local"
        if not self.local_model:
            self.local_model = "base"
        if self.cloud_provider != "gemini":
            self.cloud_provider = "gemini"
        if not self.cloud_model or self.cloud_model == "gemini-3.1-flash-lite":
            self.cloud_model = "gemini-2.5-flash"
        return self


class MCPServerConfig(BaseModel):
    name: str = Field(..., pattern=r"^[a-zA-Z0-9_-]{1,32}$")
    enabled: bool = True
    transport: Literal["stdio", "streamable_http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    startup_timeout_ms: int = 8000
    call_timeout_ms: int = 30000
    reconnect_on_unhealthy: bool = True
    allow_list: list[str] = Field(default_factory=list)
    trusted_tools: list[str] = Field(default_factory=list)
    tool_risk_overrides: dict[str, Literal["low", "medium", "high"]] = Field(default_factory=dict)
    description: str = ""


class MCPSettings(BaseModel):
    enabled: bool = True
    servers: list[MCPServerConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def server_names_must_be_unique(self) -> "MCPSettings":
        names = [server.name for server in self.servers]
        if len(names) != len(set(names)):
            raise ValueError("MCP server names must be unique")
        return self


class BrowserUseSettings(BaseModel):
    mode: Literal["auto", "managed", "system"] = "auto"
    enable_system_fallback: bool = True
    headless: bool = False
    keep_alive: bool = True
    dom_inspection_engine: Literal["auto", "enhanced", "legacy"] = "auto"
    paint_order_filtering: bool = True
    cross_origin_iframes: bool = False
    max_iframes: int = Field(5, ge=0, le=20)
    max_iframe_depth: int = Field(2, ge=0, le=5)
    system_connection_strategy: Literal["auto", "attach", "launch"] = "auto"
    system_cdp_url: str = "http://127.0.0.1:9222"
    managed_profile_dir: str = Field(
        default_factory=lambda: _default_browser_path(_BROWSER_MANAGED_PROFILE_DIR)
    )
    downloads_dir: str = Field(
        default_factory=lambda: _default_browser_path(_BROWSER_DOWNLOADS_DIR)
    )
    screenshots_dir: str = Field(
        default_factory=lambda: _default_browser_path(_BROWSER_SCREENSHOTS_DIR)
    )
    traces_dir: str = Field(
        default_factory=lambda: _default_browser_path(_BROWSER_TRACES_DIR)
    )
    system_profile_directory: str = ""
    allowed_domains: list[str] = Field(default_factory=list)


class MemorySettings(BaseModel):
    enabled: bool = True
    auto_learn: bool = True
    curate_on_session_close: bool = True
    write_policy: Literal["off", "manual", "auto_with_review", "auto_reviewed"] = "auto_with_review"
    retrieval_limit: int = Field(6, ge=1, le=20)
    max_injected_chars: int = Field(2500, ge=500, le=10000)
    min_confidence: float = Field(0.75, ge=0.0, le=1.0)
    min_relevance_score: float = Field(0.15, ge=0.0, le=1.0)
    maintenance_cooldown_hours: int = Field(24, ge=0, le=168)


class ToolSettings(BaseModel):
    skills: dict[str, bool] = Field(default_factory=lambda: dict(DEFAULT_SKILLS))

    @field_validator("skills", mode="before")
    @classmethod
    def normalize_legacy_skill_names(cls, value: Any) -> dict[str, bool]:
        if not isinstance(value, dict):
            return dict(DEFAULT_SKILLS)
        return _normalize_skill_flags(value, include_defaults=True)


class PermissionSettings(BaseModel):
    mode: Literal["default", "full_access", "custom"] = "default"
    confirmations: ConfirmationSettings = Field(default_factory=ConfirmationSettings)
    blocked_roots: list[str] = Field(default_factory=lambda: list(DEFAULT_BLOCKED_ROOTS))
    path_rules: list[PathRule] = Field(default_factory=_default_path_rules)
    app_rules: list[AppRule] = Field(default_factory=list)
    allow_delete: bool = False
    dangerous_actions_require_confirm: bool = True
    allow_screen_fallback: bool = False
    custom_profile: PermissionProfileSettings = Field(default_factory=PermissionProfileSettings)


class SandboxResourceLimits(BaseModel):
    timeout_seconds: int = Field(120, ge=1, le=14400)
    memory_mb: int = Field(1024, ge=128, le=32768)
    cpus: float = Field(1.0, ge=0.1, le=16.0)
    pids: int = Field(128, ge=16, le=4096)
    max_output_bytes: int = Field(1048576, ge=4096, le=104857600)
    max_workspace_mb: int = Field(1024, ge=16, le=102400)


class SandboxNetworkSettings(BaseModel):
    default: Literal["deny", "allow_with_approval", "allow"] = "deny"
    allow_domains: list[str] = Field(default_factory=list)


class SandboxDockerSettings(BaseModel):
    enabled: bool = True
    image: str = "python:3.12-slim"
    extra_images: list[str] = Field(default_factory=list)
    pull_policy: Literal["never", "missing", "always"] = "missing"
    read_only_root: bool = True
    no_new_privileges: bool = True


class SandboxLocalRestrictedSettings(BaseModel):
    enabled: bool = True
    use_job_object: bool = True
    kill_process_tree_on_timeout: bool = True
    strip_environment: bool = True


class SandboxWslSettings(BaseModel):
    enabled: bool = False
    distro: str = ""
    note_network_isolation_is_advisory: bool = True


class SandboxSettings(BaseModel):
    enabled: bool = True
    mode: Literal["off", "disabled", "auto", "enforce", "host", "docker", "local_restricted", "wsl"] = "auto"
    default_profile: Literal["standard", "untrusted", "project_write", "host_required"] = "standard"
    require_strong_for_untrusted: bool = True
    default_write_strategy: Literal["discard", "copy_out", "direct_rw"] = "copy_out"
    allowed_bind_roots: list[str] = Field(default_factory=list)
    blocked_bind_roots: list[str] = Field(default_factory=lambda: list(DEFAULT_BLOCKED_ROOTS))
    preserve_artifacts_days: int = Field(14, ge=1, le=365)
    resources: SandboxResourceLimits = Field(default_factory=SandboxResourceLimits)
    network: SandboxNetworkSettings = Field(default_factory=SandboxNetworkSettings)
    docker: SandboxDockerSettings = Field(default_factory=SandboxDockerSettings)
    local_restricted: SandboxLocalRestrictedSettings = Field(default_factory=SandboxLocalRestrictedSettings)
    wsl: SandboxWslSettings = Field(default_factory=SandboxWslSettings)


class IdentitySettings(BaseModel):
    agent_name: str = Field(DEFAULT_AGENT_NAME, max_length=80)
    user_name: str = Field("", max_length=80)
    user_identity: str = Field("", max_length=1000)
    communication_style: str = Field("", max_length=1000)

    @field_validator(
        "agent_name",
        "user_name",
        "user_identity",
        "communication_style",
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @model_validator(mode="after")
    def default_blank_agent_name(self) -> "IdentitySettings":
        if not self.agent_name or self.agent_name == LEGACY_AGENT_NAME:
            self.agent_name = DEFAULT_AGENT_NAME
        return self


class AgentSettings(BaseModel):
    llm: LLMSettings = Field(default_factory=LLMSettings)
    speech_to_text: SpeechToTextSettings = Field(default_factory=SpeechToTextSettings)
    mcp: MCPSettings = Field(default_factory=MCPSettings)
    browser: BrowserUseSettings = Field(default_factory=BrowserUseSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    permissions: PermissionSettings = Field(default_factory=PermissionSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    identity: IdentitySettings = Field(default_factory=IdentitySettings)


def confirmation_settings_for_mode(
    mode: Literal["default", "full_access", "custom"],
    *,
    dangerous_actions_require_confirm: bool = True,
) -> ConfirmationSettings:
    if mode == "default":
        return ConfirmationSettings(mutate=True, delete=True, launch_app=True, click=True, type=True)
    if mode == "full_access":
        return ConfirmationSettings(mutate=False, delete=True, launch_app=False, click=False, type=False)
    return ConfirmationSettings(
        mutate=False,
        delete=dangerous_actions_require_confirm,
        launch_app=False,
        click=False,
        type=False,
    )


def _normalize_mode(value: str | None) -> Literal["default", "full_access", "custom"]:
    if value == "user_config":
        return "custom"
    if value in {"default", "full_access", "custom"}:
        return value
    return "default"


def _legacy_tools_to_skills(payload: dict[str, Any]) -> dict[str, bool]:
    tools = payload.get("tools", {}) if isinstance(payload.get("tools"), dict) else {}
    windows_tools = bool(tools.get("windows_tools", True))
    windows_controller = bool(tools.get("windows_controller", True))
    return {
        "core": True,
        "exec": True,
        "computer-use": windows_tools or windows_controller,
        "filesystem": windows_controller,
        "memory": True,
        "skill-creator": False,
        "background-check": False,
        "browser-use": True,
    }


def _split_skill_frontmatter(raw: str) -> dict[str, Any]:
    if not raw.startswith("---"):
        return {}
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}
    loaded = yaml.safe_load(parts[1]) or {}
    return loaded if isinstance(loaded, dict) else {}


def _local_skill_names() -> set[str]:
    skills_dir = Path(__file__).resolve().parents[1] / "skills"
    names = set(DEFAULT_SKILLS) | {"scheduling"}
    if not skills_dir.exists():
        return names
    for skill_md in skills_dir.glob("*/SKILL.md"):
        try:
            frontmatter = _split_skill_frontmatter(skill_md.read_text(encoding="utf-8"))
        except Exception:
            continue
        name = str(frontmatter.get("name") or skill_md.parent.name.replace("_", "-")).strip()
        if name:
            names.add(name)
    return names


def _normalize_skill_flags(skills: dict[str, Any], *, include_defaults: bool = False) -> dict[str, bool]:
    normalized: dict[str, bool] = dict(DEFAULT_SKILLS) if include_defaults else {}
    known_skills = _local_skill_names()
    for key, value in skills.items():
        canonical_key = LEGACY_SKILL_ALIASES.get(str(key), str(key))
        if canonical_key in REMOVED_SKILLS:
            continue
        if canonical_key in known_skills:
            normalized[canonical_key] = bool(value)
    return normalized


def _permission_profile_from_payload(permissions: dict[str, Any]) -> dict[str, Any]:
    defaults = PermissionProfileSettings().model_dump()
    return {
        "confirmations": permissions.get("confirmations", defaults["confirmations"]),
        "blocked_roots": permissions.get("blocked_roots", defaults["blocked_roots"]),
        "path_rules": permissions.get("path_rules", defaults["path_rules"]),
        "app_rules": permissions.get("app_rules", defaults["app_rules"]),
        "allow_delete": bool(permissions.get("allow_delete", defaults["allow_delete"])),
        "dangerous_actions_require_confirm": bool(
            permissions.get(
                "dangerous_actions_require_confirm",
                defaults["dangerous_actions_require_confirm"],
            )
        ),
        "allow_screen_fallback": bool(
            permissions.get("allow_screen_fallback", defaults["allow_screen_fallback"])
        ),
    }


def _apply_permission_profile(permissions: dict[str, Any], profile: dict[str, Any]) -> None:
    for key in (
        "confirmations",
        "blocked_roots",
        "path_rules",
        "app_rules",
        "allow_delete",
        "dangerous_actions_require_confirm",
        "allow_screen_fallback",
    ):
        permissions[key] = profile[key]


def _normalize_browser_payload(browser: Any) -> dict[str, Any]:
    defaults = BrowserUseSettings().model_dump()
    if not isinstance(browser, dict):
        return defaults

    merged = dict(defaults)
    merged.update(
        {
            key: value
            for key, value in browser.items()
            if key in defaults
        }
    )
    legacy_artifacts = {
        "downloads_dir": (
            _LEGACY_BROWSER_DOWNLOADS_DIR,
            _LEGACY_PUBLIC_DOWNLOADS_DIR,
        ),
        "screenshots_dir": (
            _LEGACY_BROWSER_SCREENSHOTS_DIR,
            _LEGACY_PUBLIC_SCREENSHOTS_DIR,
        ),
    }
    for key, legacy_paths in legacy_artifacts.items():
        current = os.path.normcase(os.path.normpath(str(merged.get(key) or "")))
        if any(current == os.path.normcase(os.path.normpath(str(path))) for path in legacy_paths):
            merged[key] = defaults[key]
    for key in ("managed_profile_dir", "downloads_dir", "screenshots_dir", "traces_dir"):
        if not merged.get(key):
            merged[key] = defaults[key]
        Path(str(merged[key])).mkdir(parents=True, exist_ok=True)
    merged["system_cdp_url"] = str(merged.get("system_cdp_url", defaults["system_cdp_url"]) or "").strip()
    return merged


def build_default_agent_settings(base_settings) -> AgentSettings:
    mode = "default"
    provider = base_settings.model_provider if base_settings.model_provider in {"openai", "deepseek", "gemini"} else "openai"
    model_name = str(getattr(base_settings, "model_name", "") or "").strip()
    if provider == "gemini" and model_name not in GEMINI_CHAT_MODELS:
        model_name = DEFAULT_GEMINI_CHAT_MODEL
    return AgentSettings(
        llm=LLMSettings(
            provider=provider,
            model_name=model_name or base_settings.model_name,
            reasoning_effort=(
                base_settings.reasoning_effort
                if base_settings.reasoning_effort in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
                else "medium"
            ),
            vision_fallback_enabled=bool(getattr(base_settings, "vision_fallback_enabled", True)),
            vision_fallback_model=str(
                getattr(base_settings, "vision_fallback_model", DEFAULT_VISION_FALLBACK_MODEL)
                or DEFAULT_VISION_FALLBACK_MODEL
            ),
            vision_fallback_max_output_tokens=int(
                getattr(
                    base_settings,
                    "vision_fallback_max_output_tokens",
                    DEFAULT_VISION_FALLBACK_MAX_OUTPUT_TOKENS,
                )
                or DEFAULT_VISION_FALLBACK_MAX_OUTPUT_TOKENS
            ),
        ),
        tools=ToolSettings(skills=dict(DEFAULT_SKILLS)),
        permissions=PermissionSettings(mode=mode, confirmations=confirmation_settings_for_mode(mode)),
    )


def _legacy_policy_to_permissions(legacy: dict[str, Any]) -> PermissionSettings:
    mode = _normalize_mode(str(legacy.get("mode", "default")))
    dangerous = bool(legacy.get("dangerous_actions_require_confirm", True))
    permitted_roots = legacy["permitted_roots"] if "permitted_roots" in legacy else DEFAULT_FOLDER_RULES
    blocked_roots = legacy["blocked_roots"] if "blocked_roots" in legacy else DEFAULT_BLOCKED_ROOTS
    app_rules = [
        AppRule(
            alias=str(entry.get("alias", "")).strip(),
            display_name=str(entry.get("display_name", "")).strip(),
            exe_paths=list(entry.get("exe_paths", []) or []),
            launch_allowed=True,
            uia_allowed=True,
            screen_fallback_allowed=False,
            require_confirmation=False,
            enabled=True,
        )
        for entry in legacy.get("allowlisted_apps", []) or []
        if str(entry.get("alias", "")).strip()
    ]
    permissions = PermissionSettings(
        mode=mode,
        confirmations=confirmation_settings_for_mode(mode, dangerous_actions_require_confirm=dangerous),
        blocked_roots=[str(root) for root in blocked_roots],
        path_rules=[
            PathRule(path=str(root), read=True, write=True, delete=True, launch=False, enabled=True)
            for root in permitted_roots
        ] or _default_path_rules(),
        app_rules=app_rules,
        allow_delete=bool(legacy.get("allow_delete", False)),
        dangerous_actions_require_confirm=dangerous,
        allow_screen_fallback=False,
    )
    if permissions.mode == "custom":
        permissions.custom_profile = PermissionProfileSettings.model_validate(
            _permission_profile_from_payload(permissions.model_dump())
        )
    return permissions


def _normalize_loaded_payload(data: dict[str, Any]) -> dict[str, Any]:
    payload = dict(data)
    raw_mcp = payload.get("mcp")
    raw_mcp_bridge_enabled = None
    raw_tools_for_migration = payload.get("tools")
    if isinstance(raw_tools_for_migration, dict):
        raw_skills_for_migration = raw_tools_for_migration.get("skills", {})
        if isinstance(raw_skills_for_migration, dict) and "mcp-bridge" in raw_skills_for_migration:
            raw_mcp_bridge_enabled = bool(raw_skills_for_migration.get("mcp-bridge"))
    if not isinstance(raw_mcp, dict):
        payload["mcp"] = MCPSettings(
            enabled=True if raw_mcp_bridge_enabled is None else raw_mcp_bridge_enabled
        ).model_dump()
    else:
        payload["mcp"] = dict(raw_mcp)
        payload["mcp"].setdefault(
            "enabled",
            True if raw_mcp_bridge_enabled is None else raw_mcp_bridge_enabled,
        )
    payload["browser"] = _normalize_browser_payload(payload.get("browser"))
    if not isinstance(payload.get("memory"), dict):
        payload["memory"] = MemorySettings().model_dump()
    if not isinstance(payload.get("speech_to_text"), dict):
        payload["speech_to_text"] = SpeechToTextSettings().model_dump()
    if not isinstance(payload.get("sandbox"), dict):
        payload["sandbox"] = SandboxSettings().model_dump()
    tools = payload.get("tools")
    if not isinstance(tools, dict) or "skills" not in tools:
        payload["tools"] = {"skills": _legacy_tools_to_skills(payload)}
    else:
        raw_skills = _normalize_skill_flags(tools.get("skills", {}))
        merged_skills = dict(DEFAULT_SKILLS)
        merged_skills.update(raw_skills)
        payload["tools"] = {"skills": merged_skills}

    permissions = payload.get("permissions")
    if isinstance(permissions, dict):
        permissions = dict(permissions)
        permissions["mode"] = _normalize_mode(permissions.get("mode"))
        permissions.setdefault("allow_delete", False)
        mode = permissions["mode"]
        custom_profile = permissions.get("custom_profile")
        if not isinstance(custom_profile, dict):
            custom_profile = (
                _permission_profile_from_payload(permissions)
                if mode == "custom"
                else PermissionProfileSettings().model_dump()
            )
        permissions["custom_profile"] = custom_profile
        if mode != "custom":
            permissions["confirmations"] = confirmation_settings_for_mode(mode).model_dump()
            if mode == "default":
                permissions["allow_delete"] = False
                permissions["dangerous_actions_require_confirm"] = True
                permissions["allow_screen_fallback"] = False
            else:
                permissions["allow_delete"] = bool(permissions.get("allow_delete", False))
                permissions["dangerous_actions_require_confirm"] = False
                permissions["allow_screen_fallback"] = True
        else:
            _apply_permission_profile(permissions, _permission_profile_from_payload(custom_profile))
        payload["permissions"] = permissions

    identity = payload.get("identity")
    if not isinstance(identity, dict):
        payload["identity"] = IdentitySettings().model_dump()

    return payload


def load_agent_settings(
    base_settings=None,
    *,
    settings_path: Path | None = None,
    legacy_policy_path: Path | None = None,
) -> AgentSettings:
    path = settings_path or _SETTINGS_FILE
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        return AgentSettings.model_validate(_normalize_loaded_payload(payload))

    data = build_default_agent_settings(base_settings) if base_settings is not None else AgentSettings()

    legacy_path = legacy_policy_path
    if legacy_path is not None and legacy_path.exists():
        legacy_payload = json.loads(legacy_path.read_text(encoding="utf-8"))
        data.permissions = _legacy_policy_to_permissions(legacy_payload)

    save_agent_settings(data, settings_path=path)
    return data


def save_agent_settings(settings_data: AgentSettings, *, settings_path: Path | None = None) -> AgentSettings:
    path = settings_path or _SETTINGS_FILE
    if settings_data.permissions.mode == "custom":
        settings_data.permissions.custom_profile = PermissionProfileSettings.model_validate(
            _permission_profile_from_payload(settings_data.permissions.model_dump())
        )
    elif settings_data.permissions.mode == "full_access":
        settings_data.permissions.confirmations = confirmation_settings_for_mode("full_access")
        settings_data.permissions.dangerous_actions_require_confirm = False
        settings_data.permissions.allow_screen_fallback = True
    else:
        settings_data.permissions.confirmations = confirmation_settings_for_mode("default")
        settings_data.permissions.allow_delete = False
        settings_data.permissions.dangerous_actions_require_confirm = True
        settings_data.permissions.allow_screen_fallback = False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(settings_data.model_dump_json(indent=2), encoding="utf-8")
    return settings_data


def settings_version(settings_data: AgentSettings) -> str:
    """Stable optimistic-concurrency token for the persisted settings payload."""

    raw = settings_data.model_dump_json().encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def merge_agent_settings(current: AgentSettings, patch: dict[str, Any]) -> AgentSettings:
    merged = current.model_dump()

    def apply(dst: dict[str, Any], src: dict[str, Any]) -> None:
        for key, value in src.items():
            if isinstance(value, dict) and isinstance(dst.get(key), dict):
                apply(dst[key], value)
            else:
                dst[key] = value

    apply(merged, patch)

    patch_permissions = patch.get("permissions", {}) or {}
    sent_confirmations = "confirmations" in patch_permissions
    custom_permission_fields = {
        "confirmations",
        "blocked_roots",
        "path_rules",
        "app_rules",
        "allow_delete",
        "dangerous_actions_require_confirm",
        "allow_screen_fallback",
    }

    new_mode = merged.get("permissions", {}).get("mode")
    if new_mode and new_mode != "custom":
        preset = {
            "confirmations": confirmation_settings_for_mode(new_mode).model_dump(),
            "allow_delete": False,
            "dangerous_actions_require_confirm": new_mode == "default",
            "allow_screen_fallback": new_mode == "full_access",
        }
        edited_preset = False
        if sent_confirmations and merged["permissions"].get("confirmations") != preset["confirmations"]:
            edited_preset = True
        for key in ("allow_delete", "dangerous_actions_require_confirm", "allow_screen_fallback"):
            if key in patch_permissions and bool(merged["permissions"].get(key)) != preset[key]:
                edited_preset = True

        if edited_preset:
            merged["permissions"]["mode"] = "custom"
            merged["permissions"]["custom_profile"] = _permission_profile_from_payload(merged["permissions"])
        else:
            merged["permissions"]["confirmations"] = preset["confirmations"]
            merged["permissions"]["allow_delete"] = preset["allow_delete"]
            merged["permissions"]["dangerous_actions_require_confirm"] = preset["dangerous_actions_require_confirm"]
            merged["permissions"]["allow_screen_fallback"] = preset["allow_screen_fallback"]
    elif new_mode == "custom" and custom_permission_fields.intersection(patch_permissions):
        merged["permissions"]["custom_profile"] = _permission_profile_from_payload(merged["permissions"])

    normalized = _normalize_loaded_payload(merged)
    return AgentSettings.model_validate(normalized)


def build_runtime_namespace(base_settings, agent_settings: AgentSettings):
    return SimpleNamespace(
        model_provider=agent_settings.llm.provider,
        model_name=agent_settings.llm.model_name,
        openai_api_key=base_settings.openai_api_key,
        deepseek_api_key=getattr(base_settings, "deepseek_api_key", ""),
        deepseek_base_url=getattr(base_settings, "deepseek_base_url", "https://api.deepseek.com"),
        google_api_key=base_settings.google_api_key,
        tavily_api_key=base_settings.tavily_api_key,
        telegram_bot_token=getattr(base_settings, "telegram_bot_token", ""),
        reasoning_effort=agent_settings.llm.reasoning_effort,
        llm=SimpleNamespace(
            provider=agent_settings.llm.provider,
            model_name=agent_settings.llm.model_name,
            reasoning_effort=agent_settings.llm.reasoning_effort,
            vision_fallback_enabled=agent_settings.llm.vision_fallback_enabled,
            vision_fallback_model=agent_settings.llm.vision_fallback_model,
            vision_fallback_max_output_tokens=agent_settings.llm.vision_fallback_max_output_tokens,
            max_iterations_per_turn=agent_settings.llm.max_iterations_per_turn,
            max_turn_seconds=agent_settings.llm.max_turn_seconds,
            max_llm_call_seconds=agent_settings.llm.max_llm_call_seconds,
        ),
        speech_to_text=SimpleNamespace(**agent_settings.speech_to_text.model_dump()),
        tools=SimpleNamespace(skills=dict(agent_settings.tools.skills)),
        identity=SimpleNamespace(**agent_settings.identity.model_dump()),
        memory=SimpleNamespace(**agent_settings.memory.model_dump()),
        mcp=SimpleNamespace(
            enabled=agent_settings.mcp.enabled,
            servers=[server.model_dump() for server in agent_settings.mcp.servers],
        ),
        browser=SimpleNamespace(**agent_settings.browser.model_dump()),
        sandbox=SimpleNamespace(**agent_settings.sandbox.model_dump()),
        allow_arbitrary_app_paths=getattr(base_settings, "allow_arbitrary_app_paths", False),
    )


def api_settings_payload(base_settings, agent_settings: AgentSettings) -> dict[str, Any]:
    from app.agent.skill_loader import available_skill_payload

    runtime_settings = build_runtime_namespace(base_settings, agent_settings)
    return {
        **agent_settings.model_dump(),
        "settings_version": settings_version(agent_settings),
        "available_skills": available_skill_payload(runtime_settings),
        "api_keys": {
            "has_openai_key": bool(base_settings.openai_api_key),
            "has_deepseek_key": bool(getattr(base_settings, "deepseek_api_key", "")),
            "has_google_key": bool(base_settings.google_api_key),
            "has_tavily_key": bool(base_settings.tavily_api_key),
            "has_telegram_bot_token": bool(getattr(base_settings, "telegram_bot_token", "")),
            "has_telegram_allowlist": bool(
                getattr(base_settings, "telegram_allowed_user_ids", "")
                or getattr(base_settings, "telegram_allowed_chat_ids", "")
            ),
        },
        "telegram_allowed_user_ids": getattr(base_settings, "telegram_allowed_user_ids", ""),
        "telegram_allowed_chat_ids": getattr(base_settings, "telegram_allowed_chat_ids", ""),
    }
