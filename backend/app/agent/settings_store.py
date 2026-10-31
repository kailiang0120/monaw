"""Canonical runtime settings store for the agent."""

from __future__ import annotations

import json
import os
import hashlib
import uuid
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.agent.llm_constants import (
    CHAT_MODELS_BY_PROVIDER,
    DEFAULT_OPENAI_CHAT_MODEL,
    normalize_chat_model,
)
from app.agent.identity import DEFAULT_AGENT_NAME, LEGACY_AGENT_NAME
from app.agent.output_workspace import default_screenshots_dir
from app.agent.runtime_paths import RUNTIME_DIR, WORKSPACE_DIR

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

LEGACY_SKILL_ALIASES = {
    "desktop-control-win": "computer-use",
}


def _default_skill_flags() -> dict[str, bool]:
    from app.agent.skill_loader import default_skill_flags

    return default_skill_flags()

_BROWSER_SCREENSHOTS_DIR = default_screenshots_dir()
_LEGACY_BROWSER_SCREENSHOTS_DIR = _RUNTIME_DIR / "browser-use" / "screenshots"
_LEGACY_PUBLIC_OUTPUT_ROOT = (_USER_HOME / "Documents" if (_USER_HOME / "Documents").exists() else _USER_HOME) / "Monaw Agent"
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
    launch_app: bool = False
    click: bool = False
    type: bool = False


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
    provider: Literal["openai", "gemini", "codex"] = "openai"
    model_name: str = DEFAULT_OPENAI_CHAT_MODEL
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"] = "medium"
    max_iterations_per_turn: int = Field(200, ge=1, le=500)
    max_turn_seconds: int = Field(7200, ge=30, le=14400)
    max_llm_call_seconds: int = Field(600, ge=30, le=1800)

    @model_validator(mode="after")
    def normalize_provider_models(self) -> "LLMSettings":
        self.model_name = normalize_chat_model(self.provider, self.model_name)
        if self.provider in {"openai", "codex"} and self.model_name.startswith("gpt-6"):
            if self.reasoning_effort == "minimal" or (self.model_name in {"gpt-6-astra", "gpt-6.1-sol"} and self.reasoning_effort == "none"):
                self.reasoning_effort = "low"
        if self.provider == "codex" and self.reasoning_effort == "none":
            self.reasoning_effort = "low"
        if self.reasoning_effort == "ultra" and (self.provider != "codex" or self.model_name == "gpt-6-luna"):
            self.reasoning_effort = "max"
        if self.provider == "gemini" and self.reasoning_effort in {"none", "minimal"}:
            self.reasoning_effort = "minimal" if self.model_name == "gemini-flash-lite-latest" else "low"
        return self


class SpeechToTextSettings(BaseModel):
    # "auto" uses whichever OpenAI or Google key is saved.
    provider: Literal["auto", "openai", "gemini"] = "auto"

    @model_validator(mode="before")
    @classmethod
    def migrate_local_engine(cls, value: Any) -> Any:
        # Settings from before local Whisper was removed hold engine/cloud_*
        # fields. Choosing the cloud engine meant Gemini; local becomes auto.
        if isinstance(value, dict) and "provider" not in value:
            return {"provider": "gemini" if value.get("engine") == "cloud" else "auto"}
        if isinstance(value, dict) and value.get("provider") not in {"auto", "openai", "gemini"}:
            return {**value, "provider": "auto"}
        return value


class MCPServerConfig(BaseModel):
    server_id: str = Field("", pattern=r"^(?:[a-f0-9]{32})?$")
    name: str = Field(..., pattern=r"^[a-zA-Z0-9_-]{1,32}$")
    enabled: bool = True
    transport: Literal["stdio", "streamable_http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    credential_refs: dict[Literal["env", "headers"], dict[str, str]] = Field(default_factory=dict)
    startup_timeout_ms: int = 8000
    call_timeout_ms: int = 30000
    reconnect_on_unhealthy: bool = True
    allow_list: list[str] = Field(default_factory=list)
    trusted_tools: list[str] = Field(default_factory=list)
    tool_risk_overrides: dict[str, Literal["low", "medium", "high"]] = Field(default_factory=dict)
    description: str = ""

    @model_validator(mode="after")
    def validate_credentials(self):
        from app.security.mcp_credentials import CredentialBinding
        if not self.server_id:
            # Stable on every read of an older settings file, before its next atomic save.
            self.server_id = uuid.uuid5(uuid.NAMESPACE_URL, "monaw:mcp:" + self.name).hex
        for target in ("env", "headers"):
            keys = [*getattr(self, target), *self.credential_refs.get(target, {})]
            if len(keys) != len({key.casefold() for key in keys}):
                raise ValueError("MCP field names must be unique ignoring case")
        for target, fields in self.credential_refs.items():
            for key, ref in fields.items():
                binding = CredentialBinding(server_id=self.server_id, transport=self.transport, target=target, key=key)
                if ref != binding.reference or key in getattr(self, target):
                    raise ValueError("Invalid MCP credential binding or plaintext collision")
        return self


class MCPSettings(BaseModel):
    enabled: bool = True
    servers: list[MCPServerConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def server_names_must_be_unique(self) -> "MCPSettings":
        names = [server.name for server in self.servers]
        if len(names) != len(set(names)):
            raise ValueError("MCP server names must be unique")
        ids = [server.server_id for server in self.servers]
        if len(ids) != len(set(ids)):
            raise ValueError("MCP server identities must be unique")
        return self


class BrowserUseSettings(BaseModel):
    """Browser control drives the user's own Chrome through the Monaw extension."""

    screenshots_dir: str = Field(
        default_factory=lambda: _default_browser_path(_BROWSER_SCREENSHOTS_DIR)
    )
    # Local port the extension connects to.
    extension_port: int = Field(8436, ge=1024, le=65535)
    allowed_domains: list[str] = Field(default_factory=list)


class MemorySettings(BaseModel):
    enabled: bool = True
    auto_learn: bool = True
    retrieval_limit: int = Field(6, ge=1, le=20)
    max_injected_chars: int = Field(2500, ge=500, le=10000)
    min_relevance_score: float = Field(0.15, ge=0.0, le=1.0)
    # Searching memories by meaning. "auto" uses the saved OpenAI or Google
    # key and falls back to keywords without one; "off" is keywords only.
    semantic_search: Literal["auto", "off", "openai", "gemini", "local"] = "auto"
    # Used by "local", and by "openai" or "gemini" in place of their default model.
    embedding_model: str = ""
    embedding_endpoint: str = ""
    embedding_timeout: float = Field(5.0, ge=1.0, le=30.0)

    @model_validator(mode="before")
    @classmethod
    def migrate_embedding_mode(cls, value: Any) -> Any:
        # embedding_mode came before semantic search used the saved keys.
        # Nobody chose "keyword" on purpose (it was only the default), so it
        # becomes "auto"; "provider" meant OpenAI.
        if not isinstance(value, dict) or "embedding_mode" not in value:
            return value
        old = str(value.get("embedding_mode", ""))
        value = {key: item for key, item in value.items() if key != "embedding_mode"}
        value.setdefault("semantic_search", {"provider": "openai", "local": "local"}.get(old, "auto"))
        return value


class ToolSettings(BaseModel):
    skills: dict[str, bool] = Field(default_factory=_default_skill_flags)

    @field_validator("skills", mode="before")
    @classmethod
    def normalize_legacy_skill_names(cls, value: Any) -> dict[str, bool]:
        if not isinstance(value, dict):
            return _default_skill_flags()
        return _normalize_skill_flags(value, include_defaults=True)


class PermissionSettings(BaseModel):
    mode: Literal["default", "full_access", "auto_review", "custom"] = "default"
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
    max_copy_in_bytes: int = Field(104857600, ge=1048576, le=1073741824)
    max_copy_out_bytes: int = Field(104857600, ge=1048576, le=1073741824)


class SandboxNetworkSettings(BaseModel):
    default: Literal["deny", "allow_with_approval", "allow"] = "deny"


class SandboxDockerSettings(BaseModel):
    enabled: bool = True
    image: str = "python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a"
    pull_policy: Literal["never", "missing", "always"] = "missing"
    read_only_root: bool = True
    no_new_privileges: bool = True


class SandboxLocalRestrictedSettings(BaseModel):
    enabled: bool = True


class SandboxSettings(BaseModel):
    enabled: bool = True
    mode: Literal["off", "disabled", "auto", "enforce", "host", "docker", "local_restricted"] = "auto"
    default_profile: Literal["standard", "untrusted", "host_required"] = "standard"
    default_write_strategy: Literal["discard", "copy_out", "direct_rw"] = "copy_out"
    allowed_bind_roots: list[str] = Field(default_factory=lambda: [str(WORKSPACE_DIR)])
    blocked_bind_roots: list[str] = Field(default_factory=lambda: list(DEFAULT_BLOCKED_ROOTS))
    preserve_artifacts_days: int = Field(14, ge=1, le=365)
    resources: SandboxResourceLimits = Field(default_factory=SandboxResourceLimits)
    network: SandboxNetworkSettings = Field(default_factory=SandboxNetworkSettings)
    docker: SandboxDockerSettings = Field(default_factory=SandboxDockerSettings)
    local_restricted: SandboxLocalRestrictedSettings = Field(default_factory=SandboxLocalRestrictedSettings)


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
    mode: Literal["default", "full_access", "auto_review", "custom"],
    *,
    dangerous_actions_require_confirm: bool = True,
) -> ConfirmationSettings:
    if mode in {"default", "auto_review"}:
        return ConfirmationSettings(mutate=True, delete=True, launch_app=False, click=False, type=False)
    if mode == "full_access":
        return ConfirmationSettings(mutate=False, delete=False, launch_app=False, click=False, type=False)
    return ConfirmationSettings(
        mutate=False,
        delete=dangerous_actions_require_confirm,
        launch_app=False,
        click=False,
        type=False,
    )


def _normalize_mode(value: str | None) -> Literal["default", "full_access", "auto_review", "custom"]:
    if value == "user_config":
        return "custom"
    if value in {"default", "full_access", "auto_review", "custom"}:
        return value
    return "default"


def _legacy_tools_to_skills(payload: dict[str, Any]) -> dict[str, bool]:
    tools = payload.get("tools", {}) if isinstance(payload.get("tools"), dict) else {}
    windows_tools = bool(tools.get("windows_tools", True))
    windows_controller = bool(tools.get("windows_controller", True))
    defaults = _default_skill_flags()
    defaults["computer-use"] = windows_tools or windows_controller
    defaults["filesystem"] = windows_controller
    return defaults


def _normalize_skill_flags(skills: dict[str, Any], *, include_defaults: bool = False) -> dict[str, bool]:
    defaults = _default_skill_flags()
    normalized: dict[str, bool] = dict(defaults) if include_defaults else {}
    from app.agent.skill_loader import discovered_skill_names

    known_skills = discovered_skill_names()
    for key, value in skills.items():
        canonical_key = LEGACY_SKILL_ALIASES.get(str(key), str(key))
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

    # Keys of removed browser options (the browser-use engine, and Chrome remote
    # debugging: connection, system_profile_directory,
    # auto_approve_local_debugging) are dropped here so old settings files load.
    merged = dict(defaults)
    merged.update(
        {
            key: value
            for key, value in browser.items()
            if key in defaults
        }
    )
    current = os.path.normcase(os.path.normpath(str(merged.get("screenshots_dir") or "")))
    legacy_paths = (_LEGACY_BROWSER_SCREENSHOTS_DIR, _LEGACY_PUBLIC_SCREENSHOTS_DIR)
    if any(current == os.path.normcase(os.path.normpath(str(path))) for path in legacy_paths):
        merged["screenshots_dir"] = defaults["screenshots_dir"]
    if not merged.get("screenshots_dir"):
        merged["screenshots_dir"] = defaults["screenshots_dir"]
    Path(str(merged["screenshots_dir"])).mkdir(parents=True, exist_ok=True)
    return merged


def build_default_agent_settings(base_settings) -> AgentSettings:
    mode = "default"
    provider = base_settings.model_provider if base_settings.model_provider in {"openai", "gemini", "codex"} else "openai"
    model_name = str(getattr(base_settings, "model_name", "") or "").strip()
    model_name = normalize_chat_model(provider, model_name)
    return AgentSettings(
        llm=LLMSettings(
            provider=provider,
            model_name=model_name or base_settings.model_name,
            reasoning_effort=(
                base_settings.reasoning_effort
                if base_settings.reasoning_effort in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
                else "medium"
            ),
        ),
        tools=ToolSettings(),
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


def _normalize_llm_payload(raw_llm: Any) -> Any:
    """Migrate settings written before the provider list narrowed to OpenAI and Google.

    Retired providers (and the retired vision-fallback keys) would otherwise fail
    validation and lock the user out of their own settings file.
    """

    if not isinstance(raw_llm, dict):
        return raw_llm
    llm = {
        key: value
        for key, value in raw_llm.items()
        if not key.startswith("vision_fallback_")
    }
    # Raise the former default limits while preserving explicitly tuned budgets.
    old_limits = {"max_iterations_per_turn": 40, "max_turn_seconds": 1800, "max_llm_call_seconds": 300}
    if all(llm.get(key, value) == value for key, value in old_limits.items()):
        llm.update(max_iterations_per_turn=200, max_turn_seconds=7200, max_llm_call_seconds=600)
    provider = str(llm.get("provider") or "").strip().lower()
    if provider not in CHAT_MODELS_BY_PROVIDER:
        llm["provider"] = "openai"
        llm["model_name"] = DEFAULT_OPENAI_CHAT_MODEL
    return llm


def _normalize_loaded_payload(data: dict[str, Any]) -> dict[str, Any]:
    payload = dict(data)
    if "llm" in payload:
        payload["llm"] = _normalize_llm_payload(payload.get("llm"))
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
        # Older versions saved Chrome DevTools MCP with --autoConnect, which drove
        # Chrome through remote debugging. Browser control now goes through the
        # Monaw extension, so drop those copies instead of reviving that path.
        servers = payload["mcp"].get("servers")
        if isinstance(servers, list):
            payload["mcp"]["servers"] = [
                server for server in servers
                if not (
                    isinstance(server, dict)
                    and any("chrome-devtools-mcp" in str(arg) for arg in server.get("args") or [])
                    and "--autoConnect" in [str(arg) for arg in server.get("args") or []]
                )
            ]
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
        merged_skills = _default_skill_flags()
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
            permissions["allow_delete"] = True
            if mode in {"default", "auto_review"}:
                permissions["dangerous_actions_require_confirm"] = True
                permissions["allow_screen_fallback"] = True
            else:
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
        settings_data.permissions.allow_delete = True
        settings_data.permissions.dangerous_actions_require_confirm = False
        settings_data.permissions.allow_screen_fallback = True
    else:
        settings_data.permissions.confirmations = confirmation_settings_for_mode(settings_data.permissions.mode)
        settings_data.permissions.allow_delete = True
        settings_data.permissions.dangerous_actions_require_confirm = True
        settings_data.permissions.allow_screen_fallback = True
    path.parent.mkdir(parents=True, exist_ok=True)
    # Replace atomically so migration cannot leave a truncated settings file.
    # This is the new canonical file, never a plaintext backup/export.
    fd, temporary = tempfile.mkstemp(prefix=".settings-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(settings_data.model_dump_json(indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    try:
        from app.agent.sandbox.capabilities import invalidate_capability_cache

        invalidate_capability_cache()
    except ImportError:
        pass
    return settings_data


def settings_version(settings_data: AgentSettings) -> str:
    """Stable optimistic-concurrency token for the persisted settings payload."""

    raw = settings_data.model_dump_json().encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def merge_agent_settings(current: AgentSettings, patch: dict[str, Any]) -> AgentSettings:
    merged = current.model_dump()
    patch = {**patch}
    if "servers" in (patch.get("mcp") or {}):
        from app.security.mcp_credentials import SECRET_NAME, validate_secret
        existing = {s.server_id: s for s in current.mcp.servers}
        by_name = {s.name: s for s in current.mcp.servers}
        servers = []
        for raw in patch["mcp"]["servers"]:
            raw = dict(raw)
            old = existing.get(raw.get("server_id")) or by_name.get(raw.get("name"))
            if old:
                if raw.get("server_id") and raw["server_id"] != old.server_id:
                    raise ValueError("MCP server identity cannot be rebound")
                raw["server_id"] = old.server_id
                if raw.get("credential_refs", old.credential_refs) != old.credential_refs:
                    raise ValueError("Use credential controls to change stored credentials")
                raw["credential_refs"] = old.credential_refs
                for target in ("env", "headers"):
                    # Hidden legacy credentials survive ordinary whole-settings saves.
                    hidden = {k: v for k, v in getattr(old, target).items() if SECRET_NAME.search(k)}
                    if target in raw:
                        raw[target] = {**hidden, **raw[target]}
                    else:
                        raw[target] = dict(getattr(old, target))
            elif raw.get("credential_refs"):
                raise ValueError("Credential references cannot be attached to another server")
            for target in ("env", "headers"):
                for key, value in raw.get(target, {}).items():
                    if SECRET_NAME.search(key) and value != (getattr(old, target).get(key) if old else None):
                        raise ValueError("Use encrypted MCP credential controls for new or changed secret fields")
                    if value:
                        validate_secret(value)
            servers.append(raw)
        patch["mcp"] = {**patch["mcp"], "servers": servers}

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
            "allow_delete": True,
            "dangerous_actions_require_confirm": new_mode in {"default", "auto_review"},
            "allow_screen_fallback": True,
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
        google_api_key=base_settings.google_api_key,
        tavily_api_key=base_settings.tavily_api_key,
        telegram_bot_token=getattr(base_settings, "telegram_bot_token", ""),
        reasoning_effort=agent_settings.llm.reasoning_effort,
        llm=SimpleNamespace(
            provider=agent_settings.llm.provider,
            model_name=agent_settings.llm.model_name,
            reasoning_effort=agent_settings.llm.reasoning_effort,
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
    payload = agent_settings.model_dump()
    from app.security.mcp_credentials import SECRET_NAME
    for server in payload["mcp"]["servers"]:
        server["legacy_secret_fields"] = {target: [k for k in server[target] if SECRET_NAME.search(k)]
                                          for target in ("env", "headers")}
        for target in ("env", "headers"):
            server[target] = {k: v for k, v in server[target].items() if not SECRET_NAME.search(k)}
    return {
        **payload,
        "permissions_config_path": str(_SETTINGS_FILE),
        "settings_version": settings_version(agent_settings),
        "available_skills": available_skill_payload(runtime_settings),
        "api_keys": {
            "has_openai_key": bool(base_settings.openai_api_key),
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
