"""Authenticated desktop entry points for portable skill installation."""

import asyncio
import zipfile

import httpx
import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.agent.skill_import import install_import, prepare_import
from app.agent.settings_store import api_settings_payload, load_agent_settings
from app.agent.ui_events import publish_ui_event
from app.config import settings
from app.schemas import AgentSettingsPayload

router = APIRouter()


class SkillSource(BaseModel):
    source: str = Field(default="", max_length=4096)
    filename: str = Field(default="", max_length=200)
    data_base64: str = Field(default="", max_length=9 * 1024 * 1024)


class SkillInstall(BaseModel):
    token: str = Field(min_length=32, max_length=32)
    skills: list[str] = Field(min_length=1, max_length=200)


class SkillCandidate(BaseModel):
    id: str
    name: str
    description: str
    files: int
    installed: bool


class SkillImportPreview(BaseModel):
    token: str
    skills: list[SkillCandidate]


class SkillInstallResult(BaseModel):
    installed: list[str]
    settings: AgentSettingsPayload


@router.post("/skills/inspect", response_model=SkillImportPreview)
async def inspect_skills(body: SkillSource, request: Request):
    """Preview files in a local folder, uploaded ZIP, or public GitHub source."""
    if not body.source.strip() and not body.data_base64:
        raise HTTPException(422, "Enter a GitHub source or choose a skill folder or ZIP.")
    try:
        return await asyncio.to_thread(
            prepare_import,
            request.state.control_session.session_id,
            source=body.source.strip(),
            filename=body.filename,
            data_base64=body.data_base64,
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(422, "Could not download this public GitHub repository. Check the URL and GitHub rate limits.") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(422, "Could not reach GitHub. Check your connection and try again.") from exc
    except (ValueError, OSError, zipfile.BadZipFile, RuntimeError, yaml.YAMLError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/skills/install", response_model=SkillInstallResult)
async def install_skills(body: SkillInstall, request: Request):
    """Install selected preview entries and return fresh settings for immediate use."""
    try:
        names = await asyncio.to_thread(
            install_import,
            request.state.control_session.session_id,
            body.token,
            body.skills,
        )
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc
    publish_ui_event("settings.changed", {"sections": ["tools"], "reason": "skills_installed"})
    return {"installed": names, "settings": api_settings_payload(settings, load_agent_settings(settings))}
