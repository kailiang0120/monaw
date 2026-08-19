import asyncio
import logging

from fastapi import APIRouter, HTTPException

from app.agent.controller_policy import (
    AppEntry,
    PermissionMode,
    add_allowlisted_app,
    load_policy,
    remove_allowlisted_app,
    save_policy,
    set_permission_mode,
)
from app.agent.model_catalog import model_options_payload
from app.agent.settings_store import (
    api_settings_payload,
    load_agent_settings,
    merge_agent_settings,
    save_agent_settings,
    settings_version,
)
from app.agent.ui_events import publish_ui_event
from app.agent.speech_to_text import (
    SpeechToTextDependencyMissing,
    delete_stt_model,
    download_default_stt_model,
    offload_stt_model,
    speech_to_text_status,
)
from app.agent.workspace_instructions import (
    reset_workspace_instruction_file,
    save_workspace_instruction_content,
    workspace_instruction_payload,
)
from app.config import settings
from app.schemas import (
    AgentSettingsPayload,
    AppEntryCreate,
    AppEntryOut,
    ControllerPolicyOut,
    ControllerPolicyMarkdownPayload,
    ControllerPolicyUpdate,
    ModelOptionsPayload,
    OkResponse,
    SettingsUpdate,
    SpeechToTextStatusPayload,
    WorkspaceInstructionsPayload,
    WorkspaceInstructionsUpdate,
)
from app.skills.mcp_bridge.connection import restart_enabled_mcp_servers

router = APIRouter()
logger = logging.getLogger(__name__)


def reset_runtime_cache(*, reset_mcp: bool = True, reset_browser: bool = False) -> None:
    from app.agent.runtime import reset_runtime_cache as runtime_reset

    runtime_reset(reset_mcp=reset_mcp, reset_browser=reset_browser)


def _normalize_permission_mode(value: str) -> str:
    return "custom" if value == "user_config" else value


def _settings_patch_from_body(body: SettingsUpdate) -> dict:
    patch: dict = {}
    if body.llm is not None:
        patch["llm"] = body.llm.model_dump(exclude_unset=True)
    if body.speech_to_text is not None:
        patch["speech_to_text"] = body.speech_to_text.model_dump(exclude_unset=True)
    if body.mcp is not None:
        patch["mcp"] = body.mcp.model_dump(exclude_unset=True)
    if body.browser is not None:
        patch["browser"] = body.browser.model_dump(exclude_unset=True)
    if body.memory is not None:
        patch["memory"] = body.memory.model_dump(exclude_unset=True)
    if body.tools is not None:
        patch["tools"] = body.tools.model_dump(exclude_unset=True)
    if body.permissions is not None:
        permissions = body.permissions.model_dump(exclude_unset=True)
        if "mode" in permissions:
            permissions["mode"] = _normalize_permission_mode(permissions["mode"])
        patch["permissions"] = permissions
    if body.sandbox is not None:
        patch["sandbox"] = body.sandbox.model_dump(exclude_unset=True)
    if body.identity is not None:
        patch["identity"] = body.identity.model_dump(exclude_unset=True)

    if (
        body.model_provider is not None
        or body.model_name is not None
        or body.reasoning_effort is not None
    ):
        patch.setdefault("llm", {})
        if body.model_provider is not None:
            patch["llm"]["provider"] = body.model_provider
        if body.model_name is not None:
            patch["llm"]["model_name"] = body.model_name
        if body.reasoning_effort is not None:
            patch["llm"]["reasoning_effort"] = body.reasoning_effort

    if body.controller_permission_mode is not None:
        patch.setdefault("permissions", {})
        patch["permissions"]["mode"] = _normalize_permission_mode(body.controller_permission_mode)

    return patch


@router.put("/settings", response_model=AgentSettingsPayload)
async def update_settings(body: SettingsUpdate):
    changed = False
    runtime_changed = False
    mcp_changed = False
    browser_changed = False
    telegram_changed = False
    patch = _settings_patch_from_body(body)
    current = load_agent_settings(settings)
    current_version = settings_version(current)
    if body.expected_settings_version is not None and body.expected_settings_version != current_version:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "settings_version_conflict",
                "message": "Settings were changed by another session. Reload settings and try again.",
                "details": {
                    "current_settings_version": current_version,
                },
            },
        )
    updated = current
    if patch:
        updated = merge_agent_settings(current, patch)
        if updated != current:
            save_agent_settings(updated)
            changed = True
            runtime_changed = True
            changed_sections = {
                section
                for section in patch
                if getattr(updated, section) != getattr(current, section)
            }
            mcp_changed = "mcp" in changed_sections
            browser_changed = "browser" in changed_sections
            if "tools" in changed_sections:
                before_browser_skill = bool(current.tools.skills.get("browser-use", False))
                after_browser_skill = bool(updated.tools.skills.get("browser-use", False))
                browser_changed = browser_changed or before_browser_skill != after_browser_skill

    def assign_if_changed(name: str, value: str | None, *, default: str | None = None) -> bool:
        if value is None:
            return False
        next_value = value if value or default is None else default
        if getattr(settings, name) == next_value:
            return False
        setattr(settings, name, next_value)
        return True

    if body.model_provider is not None and assign_if_changed("model_provider", body.model_provider):
        changed = True
        runtime_changed = True
    if assign_if_changed("openai_api_key", body.openai_api_key):
        changed = True
        runtime_changed = True
    if assign_if_changed("google_api_key", body.google_api_key):
        changed = True
        runtime_changed = True
    if assign_if_changed("tavily_api_key", body.tavily_api_key):
        changed = True
        runtime_changed = True
    if assign_if_changed("telegram_bot_token", body.telegram_bot_token):
        changed = True
        runtime_changed = True
        telegram_changed = True
    if assign_if_changed("telegram_allowed_user_ids", body.telegram_allowed_user_ids):
        changed = True
        telegram_changed = True
    if assign_if_changed("telegram_allowed_chat_ids", body.telegram_allowed_chat_ids):
        changed = True
        telegram_changed = True
    if assign_if_changed("model_name", body.model_name):
        changed = True
        runtime_changed = True
    if assign_if_changed("reasoning_effort", body.reasoning_effort):
        changed = True
        runtime_changed = True
    if body.controller_permission_mode is not None:
        mapped = (
            PermissionMode.USER_CONFIG
            if body.controller_permission_mode in {"custom", "user_config"}
            else PermissionMode(body.controller_permission_mode)
        )
        set_permission_mode(mapped)
    if changed:
        logger.info(
            "Settings updated provider=%s model=%s reasoning=%s runtime_changed=%s mcp_changed=%s browser_changed=%s",
            updated.llm.provider,
            updated.llm.model_name,
            updated.llm.reasoning_effort,
            runtime_changed,
            mcp_changed,
            browser_changed,
        )
        if runtime_changed or mcp_changed or browser_changed:
            reset_runtime_cache(reset_mcp=mcp_changed, reset_browser=browser_changed)
        if mcp_changed:
            restart_enabled_mcp_servers(updated)
        if telegram_changed:
            try:
                from app.integrations.telegram.service import restart_telegram_bot

                await restart_telegram_bot(settings.telegram_bot_token)
            except Exception:
                logger.exception("Failed to restart Telegram bot after settings update.")
        publish_ui_event("settings.changed", {"settings_version": settings_version(updated)})
    return api_settings_payload(settings, updated)


@router.get("/settings", response_model=AgentSettingsPayload)
async def get_settings():
    return api_settings_payload(settings, load_agent_settings(settings))


@router.get("/settings/model-options", response_model=ModelOptionsPayload)
async def get_model_options():
    return model_options_payload()


@router.get("/settings/workspace-instructions", response_model=WorkspaceInstructionsPayload)
async def get_workspace_instructions():
    return workspace_instruction_payload()


@router.put("/settings/workspace-instructions", response_model=WorkspaceInstructionsPayload)
async def update_workspace_instructions(body: WorkspaceInstructionsUpdate):
    try:
        return save_workspace_instruction_content(body.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/settings/workspace-instructions", response_model=WorkspaceInstructionsPayload)
async def reset_workspace_instructions():
    return reset_workspace_instruction_file()


@router.get("/settings/speech-to-text", response_model=SpeechToTextStatusPayload)
async def get_speech_to_text_status():
    return speech_to_text_status()


@router.post("/settings/speech-to-text/download", response_model=SpeechToTextStatusPayload)
async def download_speech_to_text_model():
    try:
        return await asyncio.to_thread(download_default_stt_model)
    except SpeechToTextDependencyMissing as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to download speech-to-text model.")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/settings/speech-to-text/offload", response_model=SpeechToTextStatusPayload)
async def offload_speech_to_text_model():
    return offload_stt_model()


@router.delete("/settings/speech-to-text/model", response_model=SpeechToTextStatusPayload)
async def delete_speech_to_text_model():
    return delete_stt_model()


@router.get("/settings/controller-policy", response_model=ControllerPolicyOut)
async def get_controller_policy():
    state = load_policy()
    return ControllerPolicyOut(
        mode=state.mode.value,
        permitted_roots=state.permitted_roots,
        blocked_roots=state.blocked_roots,
        allow_delete=state.allow_delete,
        dangerous_actions_require_confirm=state.dangerous_actions_require_confirm,
        allowlisted_apps=[
            AppEntryOut(
                alias=a.alias,
                display_name=a.display_name,
                exe_paths=a.exe_paths,
                added_at=a.added_at,
            )
            for a in state.allowlisted_apps
        ],
    )


@router.put("/settings/controller-policy", response_model=OkResponse)
async def update_controller_policy(body: ControllerPolicyUpdate):
    state = load_policy()
    if body.mode is not None:
        state.mode = PermissionMode.USER_CONFIG if body.mode in {"custom", "user_config"} else PermissionMode(body.mode)
    if body.permitted_roots is not None:
        state.permitted_roots = body.permitted_roots
    if body.blocked_roots is not None:
        state.blocked_roots = body.blocked_roots
    if body.allow_delete is not None:
        state.allow_delete = body.allow_delete
    if body.dangerous_actions_require_confirm is not None:
        state.dangerous_actions_require_confirm = body.dangerous_actions_require_confirm
    save_policy(state)
    return {"ok": True}


@router.get("/settings/allowlisted-apps", response_model=list[AppEntryOut])
async def list_allowlisted_apps():
    state = load_policy()
    return [
        AppEntryOut(
            alias=a.alias,
            display_name=a.display_name,
            exe_paths=a.exe_paths,
            added_at=a.added_at,
        )
        for a in state.allowlisted_apps
    ]


@router.post("/settings/allowlisted-apps", response_model=AppEntryOut)
async def create_allowlisted_app(body: AppEntryCreate):
    try:
        entry = AppEntry(
            alias=body.alias,
            display_name=body.display_name,
            exe_paths=body.exe_paths,
        )
        state = add_allowlisted_app(entry)
        added = next(a for a in state.allowlisted_apps if a.alias == body.alias)
        return AppEntryOut(
            alias=added.alias,
            display_name=added.display_name,
            exe_paths=added.exe_paths,
            added_at=added.added_at,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/settings/allowlisted-apps/{alias}", response_model=OkResponse)
async def delete_allowlisted_app(alias: str):
    remove_allowlisted_app(alias)
    return {"ok": True}


@router.get("/settings/controller-policy-markdown", response_model=ControllerPolicyMarkdownPayload)
async def get_controller_policy_markdown():
    from app.agent.controller_policy import _ALLOWLIST_FILE, _POLICY_FILE

    policy_md = _POLICY_FILE.read_text(encoding="utf-8") if _POLICY_FILE.exists() else ""
    allowlist_md = _ALLOWLIST_FILE.read_text(encoding="utf-8") if _ALLOWLIST_FILE.exists() else ""
    return {"policy_markdown": policy_md, "allowlist_markdown": allowlist_md}
