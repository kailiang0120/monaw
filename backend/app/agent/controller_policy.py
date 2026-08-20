"""Controller policy compatibility layer over canonical runtime settings.

The source of truth is backend/.runtime/settings.json. Legacy policy JSON and
Markdown files are still generated for transparency and backward-compatible
tests, but they are no longer the authoritative store.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.agent.settings_store import (
    AgentSettings,
    AppRule,
    PathRule,
    PermissionSettings,
    confirmation_settings_for_mode,
    load_agent_settings,
    save_agent_settings,
)
from app.agent.runtime_paths import RUNTIME_DIR, WORKSPACE_DIR

_USER_HOME = Path(os.path.expanduser("~"))
_POLICY_DIR = RUNTIME_DIR / "policy"
_POLICY_DIR.mkdir(parents=True, exist_ok=True)

_POLICY_FILE = _POLICY_DIR / "controller_policy.md"
_ALLOWLIST_FILE = _POLICY_DIR / "allowlisted_apps.md"

BLOCKED_PROCESSES = frozenset({
    "keepass.exe", "1password.exe", "lastpass.exe",
    "mmc.exe", "secpol.msc", "gpedit.msc",
    "regedit.exe", "taskmgr.exe", "procexp.exe",
    "cmd.exe", "powershell.exe", "pwsh.exe",
    "windowsterminal.exe", "wt.exe",
    "codex.exe", "monaw.exe",
})


def normalize_app_alias(alias: str) -> str:
    """Normalize user/model-provided app aliases for consistent matching."""
    value = (alias or "").strip()
    # Handle repeated wrapping quotes like "'docker desktop'" or "\"powershell\""
    while len(value) >= 2 and (
        (value[0] == "'" and value[-1] == "'")
        or (value[0] == '"' and value[-1] == '"')
    ):
        value = value[1:-1].strip()
    return value.lower()


def is_blocked_process_alias(alias: str) -> bool:
    """Return true when an app alias/path resolves to a blocked process name."""
    value = normalize_app_alias(alias)
    if not value:
        return False

    basename = os.path.basename(value)
    candidates = {value, basename}
    if value and not value.endswith(".exe"):
        candidates.add(f"{value}.exe")
    if basename and not basename.endswith(".exe"):
        candidates.add(f"{basename}.exe")
    return any(candidate in BLOCKED_PROCESSES for candidate in candidates)


DEFAULT_BLOCKED_ROOTS = [
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
    r"C:\$Recycle.Bin",
    str(_USER_HOME / "AppData"),
]

DEFAULT_PERMITTED_ROOTS: list[str] = []


class PermissionMode(str, Enum):
    DEFAULT = "default"
    FULL_ACCESS = "full_access"
    USER_CONFIG = "user_config"


class ActionType(str, Enum):
    READ = "read"
    MUTATE = "mutate"
    DELETE = "delete"
    LAUNCH_APP = "launch_app"
    CLICK = "click"
    TYPE = "type"
    EXEC = "exec"
    PROCESS_KILL = "process_kill"


class AppEntry(BaseModel):
    alias: str
    display_name: str = ""
    exe_paths: list[str] = Field(default_factory=list)
    added_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ControllerPolicyState(BaseModel):
    mode: PermissionMode = PermissionMode.DEFAULT
    permitted_roots: list[str] = Field(default_factory=lambda: list(DEFAULT_PERMITTED_ROOTS))
    blocked_roots: list[str] = Field(default_factory=lambda: list(DEFAULT_BLOCKED_ROOTS))
    allow_delete: bool = False
    dangerous_actions_require_confirm: bool = True
    allowlisted_apps: list[AppEntry] = Field(default_factory=list)


class PermissionDecision(BaseModel):
    allowed: bool = True
    requires_confirmation: bool = False
    requires_access_grant: bool = False
    reason: str = ""
    blocked: bool = False
    reason_code: str = ""
    policy_source: str = "settings.json"


def _requires_confirmation_for_high_risk_action(
    action: ActionType,
    *,
    mode: PermissionMode | None = None,
    mode_str: str | None = None,
    dangerous_actions_require_confirm: bool = True,
) -> bool:
    """Decide if a high-risk action needs confirmation by policy mode."""
    if action == ActionType.PROCESS_KILL:
        return True

    if action != ActionType.EXEC:
        return False

    if mode is not None:
        if mode == PermissionMode.FULL_ACCESS:
            return False
        if mode == PermissionMode.USER_CONFIG:
            return dangerous_actions_require_confirm
        return True

    if mode_str == "full_access":
        return False
    if mode_str == "custom":
        return dangerous_actions_require_confirm
    return True


def _policy_json_path() -> Path:
    return _POLICY_DIR / "controller_policy.json"


def _settings_json_path() -> Path:
    return _POLICY_DIR.parent / "settings.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _settings_mode_to_compat(mode: str) -> PermissionMode:
    if mode == "full_access":
        return PermissionMode.FULL_ACCESS
    if mode == "custom":
        return PermissionMode.USER_CONFIG
    return PermissionMode.DEFAULT


def _compat_mode_to_settings(mode: PermissionMode) -> str:
    if mode == PermissionMode.FULL_ACCESS:
        return "full_access"
    if mode == PermissionMode.USER_CONFIG:
        return "custom"
    return "default"


def _path_rules_to_roots(path_rules: list[PathRule]) -> list[str]:
    roots: list[str] = []
    for rule in path_rules:
        if rule.enabled and (rule.read or rule.write or rule.delete or rule.launch):
            roots.append(rule.path)
    return roots


def _permissions_to_state(settings_data: AgentSettings) -> ControllerPolicyState:
    perms = settings_data.permissions
    apps = [
        AppEntry(
            alias=rule.alias,
            display_name=rule.display_name,
            exe_paths=list(rule.exe_paths),
        )
        for rule in perms.app_rules
        if rule.enabled
    ]
    return ControllerPolicyState(
        mode=_settings_mode_to_compat(perms.mode),
        permitted_roots=_path_rules_to_roots(perms.path_rules),
        blocked_roots=list(perms.blocked_roots),
        allow_delete=perms.allow_delete,
        dangerous_actions_require_confirm=perms.dangerous_actions_require_confirm,
        allowlisted_apps=apps,
    )


def _merge_app_rules(existing: list[AppRule], entries: list[AppEntry]) -> list[AppRule]:
    existing_by_alias = {rule.alias.lower(): rule for rule in existing}
    merged: list[AppRule] = []
    for entry in entries:
        prior = existing_by_alias.get(entry.alias.lower())
        merged.append(
            AppRule(
                alias=entry.alias,
                display_name=entry.display_name,
                exe_paths=list(entry.exe_paths),
                launch_allowed=prior.launch_allowed if prior else True,
                uia_allowed=prior.uia_allowed if prior else True,
                screen_fallback_allowed=prior.screen_fallback_allowed if prior else False,
                require_confirmation=prior.require_confirmation if prior else False,
                enabled=prior.enabled if prior else True,
            )
        )
    return merged


def _state_to_permissions(state: ControllerPolicyState, existing: PermissionSettings | None = None) -> PermissionSettings:
    mode = _compat_mode_to_settings(state.mode)
    existing = existing or PermissionSettings()

    # Preserve existing confirmations when mode hasn't changed;
    # only regenerate from the profile on actual mode transitions.
    if existing.mode == mode:
        confirmations = existing.confirmations
    else:
        confirmations = confirmation_settings_for_mode(
            mode,
            dangerous_actions_require_confirm=state.dangerous_actions_require_confirm,
        )

    return PermissionSettings(
        mode=mode,
        confirmations=confirmations,
        blocked_roots=list(state.blocked_roots),
        path_rules=[
            PathRule(
                path=root,
                read=True,
                write=True,
                delete=True,
                launch=False,
                enabled=True,
            )
            for root in state.permitted_roots
        ],
        app_rules=_merge_app_rules(existing.app_rules, state.allowlisted_apps),
        allow_delete=state.allow_delete,
        dangerous_actions_require_confirm=state.dangerous_actions_require_confirm,
        allow_screen_fallback=existing.allow_screen_fallback,
    )


def _load_runtime_settings() -> AgentSettings:
    return load_agent_settings(
        settings_path=_settings_json_path(),
        legacy_policy_path=_policy_json_path(),
    )


def _write_policy_md(state: ControllerPolicyState) -> None:
    roots_list = "\n".join(f"- `{r}`" for r in state.permitted_roots)
    blocked_list = "\n".join(f"- `{r}`" for r in state.blocked_roots)
    content = f"""# Controller Policy

> Auto-generated. Edits here are overwritten on next save from the UI or API.

## Permission Mode

**{state.mode.value}**

- `default` - ask before each mutating action
- `full_access` - no prompt for non-destructive; still confirm deletes
- `user_config` - follow user-configured rules

## Permitted Roots

{roots_list}

## Blocked Roots

{blocked_list}

## Safety

- Dangerous actions require confirmation: **{state.dangerous_actions_require_confirm}**

---

## Changelog

- {_now_iso()} - policy saved (mode={state.mode.value}, roots={len(state.permitted_roots)})
"""
    _POLICY_FILE.write_text(content, encoding="utf-8")


def _write_allowlist_md(apps: list[AppEntry]) -> None:
    if not apps:
        rows = "| _(none)_ | | | |"
    else:
        rows = "\n".join(
            f"| {a.alias} | {a.display_name} | {', '.join(a.exe_paths[:2])} | {a.added_at} |"
            for a in apps
        )

    content = f"""# Allowlisted Applications

> Apps the agent is permitted to interact with via UI automation.
> Managed from Settings UI or API. System/sensitive apps are always blocked.

| Alias | Display Name | Exe Paths | Added |
|-------|-------------|-----------|-------|
{rows}

---

## Changelog

- {_now_iso()} - allowlist updated ({len(apps)} app(s))
"""
    _ALLOWLIST_FILE.write_text(content, encoding="utf-8")


def _read_policy_json() -> dict[str, Any]:
    path = _policy_json_path()
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    state = _permissions_to_state(_load_runtime_settings())
    _write_policy_json(state)
    return state.model_dump()


def _write_policy_json(state: ControllerPolicyState) -> None:
    _policy_json_path().write_text(state.model_dump_json(indent=2), encoding="utf-8")


def load_policy() -> ControllerPolicyState:
    state = _permissions_to_state(_load_runtime_settings())
    _write_policy_json(state)
    if not _POLICY_FILE.exists():
        _write_policy_md(state)
    if not _ALLOWLIST_FILE.exists():
        _write_allowlist_md(state.allowlisted_apps)
    return state


def save_policy(state: ControllerPolicyState) -> None:
    settings_data = _load_runtime_settings()
    settings_data.permissions = _state_to_permissions(state, settings_data.permissions)
    save_agent_settings(settings_data, settings_path=_settings_json_path())
    _write_policy_json(state)
    _write_policy_md(state)
    _write_allowlist_md(state.allowlisted_apps)


def get_permission_mode() -> PermissionMode:
    return load_policy().mode


def set_permission_mode(mode: PermissionMode) -> ControllerPolicyState:
    state = load_policy()
    state.mode = mode
    save_policy(state)
    return state


def get_allowlisted_apps() -> list[AppEntry]:
    return load_policy().allowlisted_apps


def add_allowlisted_app(entry: AppEntry) -> ControllerPolicyState:
    if is_blocked_process_alias(entry.alias):
        raise ValueError(f"'{entry.alias}' is a blocked process and cannot be allowlisted.")

    for exe in entry.exe_paths:
        if is_blocked_process_alias(exe):
            basename = os.path.basename(exe).lower()
            raise ValueError(f"'{basename}' is a blocked process and cannot be allowlisted.")

    settings_data = _load_runtime_settings()
    settings_data.permissions.app_rules = [
        rule for rule in settings_data.permissions.app_rules if rule.alias.lower() != entry.alias.lower()
    ] + [
        AppRule(
            alias=entry.alias,
            display_name=entry.display_name,
            exe_paths=list(entry.exe_paths),
            launch_allowed=True,
            uia_allowed=True,
            screen_fallback_allowed=False,
            require_confirmation=False,
            enabled=True,
        )
    ]
    save_agent_settings(settings_data, settings_path=_settings_json_path())
    state = _permissions_to_state(settings_data)
    _write_policy_json(state)
    _write_policy_md(state)
    _write_allowlist_md(state.allowlisted_apps)
    return state


def remove_allowlisted_app(alias: str) -> ControllerPolicyState:
    settings_data = _load_runtime_settings()
    settings_data.permissions.app_rules = [
        rule for rule in settings_data.permissions.app_rules if rule.alias.lower() != alias.lower()
    ]
    save_agent_settings(settings_data, settings_path=_settings_json_path())
    state = _permissions_to_state(settings_data)
    _write_policy_json(state)
    _write_policy_md(state)
    _write_allowlist_md(state.allowlisted_apps)
    return state


def update_permitted_roots(
    roots: list[str],
    *,
    new_rule: PathRule | None = None,
    merge_existing_rule: bool = False,
) -> ControllerPolicyState:
    """Replace the permitted-root view without destroying rule details.

    The compatibility policy exposes roots, but settings store richer
    ``PathRule`` entries. Keep the existing rule for every retained root and
    only synthesize a rule for a genuinely new root.
    """

    settings_data = _load_runtime_settings()
    existing = {
        canonical(rule.path): rule
        for rule in settings_data.permissions.path_rules
        if rule.enabled
    }
    new_rule_path = canonical(new_rule.path) if new_rule is not None else ""
    path_rules: list[PathRule] = []
    seen: set[str] = set()
    for root in roots:
        root_key = canonical(root)
        if root_key in seen:
            continue
        seen.add(root_key)
        prior = existing.get(root_key)
        if prior is not None:
            if merge_existing_rule and new_rule is not None and root_key == new_rule_path:
                path_rules.append(
                    prior.model_copy(
                        update={
                            "read": prior.read or new_rule.read,
                            "write": prior.write or new_rule.write,
                            "delete": prior.delete or new_rule.delete,
                            "launch": prior.launch or new_rule.launch,
                        }
                    )
                )
            else:
                path_rules.append(prior)
        elif new_rule is not None and root_key == new_rule_path:
            path_rules.append(new_rule.model_copy(update={"path": root}))
        else:
            path_rules.append(PathRule(path=root, read=True, write=True, delete=True, launch=False, enabled=True))
    settings_data.permissions.path_rules = path_rules
    save_agent_settings(settings_data, settings_path=_settings_json_path())
    state = _permissions_to_state(settings_data)
    _write_policy_json(state)
    _write_policy_md(state)
    _write_allowlist_md(state.allowlisted_apps)
    return state


def canonical(p: str) -> str:
    return str(Path(os.path.realpath(os.path.abspath(p))).resolve())


def _path_within_root(canon_path: str, root: str) -> bool:
    root_canon = canonical(root)
    try:
        return os.path.commonpath([os.path.normcase(canon_path), os.path.normcase(root_canon)]) == os.path.normcase(
            root_canon
        )
    except ValueError:
        return False


def _match_path_rule(path: str, permissions: PermissionSettings) -> PathRule | None:
    canon = canonical(path)
    matches = [
        rule for rule in permissions.path_rules
        if rule.enabled and _path_within_root(canon, rule.path)
    ]
    matches.sort(key=lambda rule: len(canonical(rule.path)), reverse=True)
    return matches[0] if matches else None


def _path_rule_allows(rule: PathRule | None, action: ActionType) -> bool:
    if rule is None or not rule.enabled:
        return False
    if action == ActionType.READ:
        return rule.read
    if action in (ActionType.MUTATE, ActionType.EXEC):
        return rule.write
    if action == ActionType.DELETE:
        return rule.delete
    if action == ActionType.LAUNCH_APP:
        return rule.launch
    return True


def is_path_permitted(path: str, state: ControllerPolicyState | None = None) -> bool:
    if state is not None:
        canon = canonical(path)
        in_blocked = any(_path_within_root(canon, root) for root in state.blocked_roots)
        return not in_blocked

    settings_data = _load_runtime_settings()
    canon = canonical(path)
    if any(_path_within_root(canon, root) for root in settings_data.permissions.blocked_roots):
        return False
    return True


def get_effective_app_rule(alias: str, settings_data: AgentSettings | None = None) -> AppRule | None:
    settings_data = settings_data or _load_runtime_settings()
    target = normalize_app_alias(alias)
    for rule in settings_data.permissions.app_rules:
        if normalize_app_alias(rule.alias) == target:
            return rule if rule.enabled else None
    return None


def is_app_allowed(alias: str, state: ControllerPolicyState | None = None) -> bool:
    target = normalize_app_alias(alias)
    if is_blocked_process_alias(target):
        return False
    if state is not None:
        return True
    rule = get_effective_app_rule(alias)
    if rule is None:
        return True
    return bool(rule.launch_allowed or rule.uia_allowed)


def is_screen_fallback_allowed(alias: str | None = None) -> bool:
    settings_data = _load_runtime_settings()
    if not settings_data.permissions.allow_screen_fallback:
        return False
    if not alias:
        return True
    rule = get_effective_app_rule(alias, settings_data)
    return bool(rule.screen_fallback_allowed) if rule else True


def _check_session_grant(target_type: str, identifier: str) -> bool:
    """Check if a session grant exists via the access grant broker."""
    try:
        from app.agent.access_grant_broker import check_session_grant
        return check_session_grant(target_type, identifier)
    except Exception:
        return False


def _is_path_in_blocked_roots(canon_path: str, blocked_roots: list[str]) -> bool:
    return any(_path_within_root(canon_path, root) for root in blocked_roots)


def _is_state_permitted_root(canon_path: str, roots: list[str]) -> bool:
    return any(_path_within_root(canon_path, root) for root in roots)


def _is_user_private_grantable_root(canon_path: str, blocked_roots: list[str]) -> bool:
    """Return true for privacy-sensitive user roots that may be user-approved.

    OS and application installation roots remain hard blocks. User-private roots
    such as AppData are hidden by default, but the user can grant access for a
    specific task/session.
    """
    user_app_data = _USER_HOME / "AppData"
    grantable_roots = {
        canonical(str(user_app_data)),
        canonical(str(user_app_data / "Local")),
        canonical(str(user_app_data / "LocalLow")),
        canonical(str(user_app_data / "Roaming")),
    }
    for root in blocked_roots:
        root_canon = canonical(root)
        if not _path_within_root(canon_path, root_canon):
            continue
        return root_canon in grantable_roots
    return False


def _browser_runtime_roots(settings_data: AgentSettings | None) -> list[str]:
    roots = [str(RUNTIME_DIR), str(WORKSPACE_DIR)]
    app_data = os.getenv("APPDATA")
    local_app_data = os.getenv("LOCALAPPDATA")
    roaming_base = Path(app_data) if app_data else _USER_HOME / "AppData" / "Roaming"
    local_base = Path(local_app_data) if local_app_data else _USER_HOME / "AppData" / "Local"
    roots.extend(
        [
            str(roaming_base / "ai-agent" / "runtime"),
            str(local_base / "ai-agent" / "runtime"),
            str(local_base / "AI Agent" / "runtime"),
        ]
    )
    browser = getattr(settings_data, "browser", None) if settings_data is not None else None
    if browser is None:
        return roots
    for field_name in ("downloads_dir", "screenshots_dir", "traces_dir", "managed_profile_dir"):
        value = str(getattr(browser, field_name, "") or "").strip()
        if value:
            roots.append(value)
    return roots


def _is_trusted_runtime_path(canon_path: str, settings_data: AgentSettings | None = None) -> bool:
    return _is_path_in_blocked_roots(canon_path, _browser_runtime_roots(settings_data))


def _compat_allowlisted_app(target_app: str, state: ControllerPolicyState) -> AppEntry | None:
    normalized_target = normalize_app_alias(target_app)
    for entry in state.allowlisted_apps:
        if normalize_app_alias(entry.alias) == normalized_target:
            return entry
    return None


def _delete_disabled_decision(policy_source: str) -> PermissionDecision:
    return PermissionDecision(
        allowed=False,
        blocked=True,
        reason="Delete operations are disabled in settings.",
        reason_code="delete_disabled",
        policy_source=policy_source,
    )


def _path_access_grant_decision(target_path: str, policy_source: str) -> PermissionDecision:
    return PermissionDecision(
        allowed=False,
        requires_access_grant=True,
        reason=f"Path '{target_path}' is not in permitted roots. User decision required.",
        reason_code="access_grant_required",
        policy_source=policy_source,
    )
def resolve_permission(
    action: ActionType,
    *,
    target_path: str = "",
    target_app: str = "",
    state: ControllerPolicyState | None = None,
) -> PermissionDecision:
    if state is not None:
        normalized_target_app = normalize_app_alias(target_app)
        compat_app_entry = _compat_allowlisted_app(normalized_target_app, state) if normalized_target_app else None
        canon = canonical(target_path) if target_path else ""
        trusted_runtime_path = bool(canon and _is_trusted_runtime_path(canon))
        path_session_granted = False

        if target_app and is_blocked_process_alias(target_app):
            return PermissionDecision(
                allowed=False,
                blocked=True,
                reason=f"App '{target_app}' is a blocked sensitive process.",
                reason_code="blocked_process",
                policy_source="compat_state",
            )

        if target_path:
            blocked_by_root = _is_path_in_blocked_roots(canon, state.blocked_roots)
            if blocked_by_root and not _is_trusted_runtime_path(canon):
                if _is_user_private_grantable_root(canon, state.blocked_roots):
                    path_session_granted = _check_session_grant("path", target_path)
                    if not _is_state_permitted_root(canon, state.permitted_roots) and not path_session_granted:
                        return PermissionDecision(
                            allowed=False,
                            requires_access_grant=True,
                            reason=f"Path '{target_path}' is inside a private blocked root. User decision required.",
                            reason_code="access_grant_required",
                            policy_source="compat_state",
                        )
                else:
                    return PermissionDecision(
                        allowed=False,
                        blocked=True,
                        reason=f"Path '{target_path}' is inside a blocked root.",
                        reason_code="blocked_root",
                        policy_source="compat_state",
                    )

        if action == ActionType.DELETE and not state.allow_delete:
            return _delete_disabled_decision("compat_state")

        if (
            target_path
            and state.mode != PermissionMode.FULL_ACCESS
            and not trusted_runtime_path
            and not _is_state_permitted_root(canon, state.permitted_roots)
            and not path_session_granted
            and not _check_session_grant("path", target_path)
        ):
            return _path_access_grant_decision(target_path, "compat_state")

        if (
            action == ActionType.LAUNCH_APP
            and normalized_target_app
            and compat_app_entry is None
            and state.mode != PermissionMode.FULL_ACCESS
            and not _check_session_grant("app", normalized_target_app)
        ):
            return PermissionDecision(
                allowed=False,
                requires_access_grant=True,
                reason=f"App '{target_app}' is not in the allow list. User decision required.",
                reason_code="access_grant_required",
                policy_source="compat_state",
            )

        if _requires_confirmation_for_high_risk_action(
            action,
            mode=state.mode,
            dangerous_actions_require_confirm=state.dangerous_actions_require_confirm,
        ):
            return PermissionDecision(
                requires_confirmation=True,
                reason=f"High-risk action '{action.value}' requires approval in current mode.",
                reason_code="confirmation_required",
                policy_source="compat_state",
            )

        if state.mode == PermissionMode.DEFAULT and action in (
            ActionType.MUTATE,
            ActionType.DELETE,
            ActionType.CLICK,
            ActionType.TYPE,
        ):
            return PermissionDecision(
                requires_confirmation=True,
                reason="Default mode: confirmation required for this action.",
                reason_code="confirmation_required",
                policy_source="compat_state",
            )

        if state.mode == PermissionMode.FULL_ACCESS and action == ActionType.DELETE and state.allow_delete:
            return PermissionDecision(
                requires_confirmation=True,
                reason="Full-access mode: delete operations still require confirmation.",
                reason_code="confirmation_required",
                policy_source="compat_state",
            )

        if (
            state.mode == PermissionMode.USER_CONFIG
            and state.dangerous_actions_require_confirm
            and action == ActionType.DELETE
            and state.allow_delete
        ):
            return PermissionDecision(
                requires_confirmation=True,
                reason="User config: dangerous actions require confirmation.",
                reason_code="confirmation_required",
                policy_source="compat_state",
            )

        return PermissionDecision(allowed=True, reason="Action permitted.", policy_source="compat_state")

    settings_data = _load_runtime_settings()
    perms = settings_data.permissions
    canon_path = canonical(target_path) if target_path else ""
    normalized_target_app = normalize_app_alias(target_app)
    matched_path_rule = _match_path_rule(canon_path, perms) if canon_path else None
    trusted_runtime_path = bool(canon_path and _is_trusted_runtime_path(canon_path, settings_data))
    path_session_granted = False

    if normalized_target_app and is_blocked_process_alias(normalized_target_app):
        return PermissionDecision(
            allowed=False,
            blocked=True,
            reason=f"App '{target_app}' is a blocked sensitive process.",
            reason_code="blocked_process",
        )

    if canon_path and _is_path_in_blocked_roots(canon_path, perms.blocked_roots) and not trusted_runtime_path:
        if _is_user_private_grantable_root(canon_path, perms.blocked_roots):
            has_persistent_grant = matched_path_rule is not None and _path_rule_allows(matched_path_rule, action)
            path_session_granted = _check_session_grant("path", target_path)
            if not has_persistent_grant and not path_session_granted:
                return PermissionDecision(
                    allowed=False,
                    requires_access_grant=True,
                    reason=f"Path '{target_path}' is inside a private blocked root. User decision required.",
                    reason_code="access_grant_required",
                )
        else:
            return PermissionDecision(
                allowed=False,
                blocked=True,
                reason=f"Path '{target_path}' is inside a blocked root.",
                reason_code="blocked_root",
            )

    app_rule = get_effective_app_rule(normalized_target_app, settings_data) if normalized_target_app else None

    if action == ActionType.DELETE and not perms.allow_delete:
        return _delete_disabled_decision("settings.json")

    if canon_path and matched_path_rule is not None and not _path_rule_allows(matched_path_rule, action):
        return PermissionDecision(
            allowed=False,
            blocked=True,
            reason=f"Path '{target_path}' is not permitted for {action.value}.",
            reason_code="path_not_permitted",
        )

    if (
        canon_path
        and perms.mode != "full_access"
        and matched_path_rule is None
        and not trusted_runtime_path
        and not path_session_granted
        and not _check_session_grant("path", target_path)
    ):
        return _path_access_grant_decision(target_path, "settings.json")

    if (
        action == ActionType.LAUNCH_APP
        and normalized_target_app
        and app_rule is None
        and perms.mode != "full_access"
        and not _check_session_grant("app", normalized_target_app)
    ):
        return PermissionDecision(
            allowed=False,
            requires_access_grant=True,
            reason=f"App '{target_app}' is not in the allow list. User decision required.",
            reason_code="access_grant_required",
        )

    if normalized_target_app and action == ActionType.LAUNCH_APP and app_rule and not app_rule.launch_allowed:
        return PermissionDecision(
            allowed=False,
            blocked=True,
            reason=f"Launching '{target_app}' is disabled in settings.",
            reason_code="launch_not_allowed",
        )

    if normalized_target_app and action in (ActionType.CLICK, ActionType.TYPE) and app_rule and not app_rule.uia_allowed:
        return PermissionDecision(
            allowed=False,
            blocked=True,
            reason=f"UI automation for '{target_app}' is disabled in settings.",
            reason_code="uia_not_allowed",
        )

    if _requires_confirmation_for_high_risk_action(
        action,
        mode_str=perms.mode,
        dangerous_actions_require_confirm=perms.dangerous_actions_require_confirm,
    ):
        return PermissionDecision(
            allowed=True,
            requires_confirmation=True,
            reason=f"High-risk action '{action.value}' requires approval in current mode.",
            reason_code="confirmation_required",
        )

    confirmations = perms.confirmations
    requires_confirmation = False
    reason = "Action permitted."

    if perms.mode == "default":
        requires_confirmation = bool(
            (action == ActionType.MUTATE and confirmations.mutate)
            or (action == ActionType.DELETE and confirmations.delete and perms.allow_delete)
            or (action == ActionType.CLICK and confirmations.click)
            or (action == ActionType.TYPE and confirmations.type)
        )
        if requires_confirmation:
            reason = "Default mode: confirmation required for this action."
    elif perms.mode == "full_access":
        requires_confirmation = bool(action == ActionType.DELETE and confirmations.delete and perms.allow_delete)
        if requires_confirmation:
            reason = "Full-access mode: delete operations still require confirmation."
    else:
        requires_confirmation = bool(
            (action == ActionType.MUTATE and confirmations.mutate)
            or (action == ActionType.DELETE and confirmations.delete and perms.allow_delete)
            or (action == ActionType.LAUNCH_APP and confirmations.launch_app)
            or (action == ActionType.CLICK and confirmations.click)
            or (action == ActionType.TYPE and confirmations.type)
        )
        if requires_confirmation:
            reason = "Custom mode: confirmation required by settings."

    if not requires_confirmation and matched_path_rule and matched_path_rule.require_confirmation:
        requires_confirmation = True
        reason = f"Path rule for '{matched_path_rule.path}' requires confirmation."
    if not requires_confirmation and app_rule and app_rule.require_confirmation:
        requires_confirmation = True
        reason = f"App rule for '{app_rule.alias}' requires confirmation."

    return PermissionDecision(
        allowed=True,
        requires_confirmation=requires_confirmation,
        reason=reason,
        reason_code="confirmation_required" if requires_confirmation else "allowed",
    )


if not _settings_json_path().exists():
    save_agent_settings(load_agent_settings(settings_path=_settings_json_path()), settings_path=_settings_json_path())
