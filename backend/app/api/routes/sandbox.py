from fastapi import APIRouter

from app.agent.sandbox.capabilities import get_sandbox_status
from app.agent.settings_store import load_agent_settings
from app.config import settings

router = APIRouter()


@router.get("/sandbox/status")
async def sandbox_status():
    runtime_settings = load_agent_settings(settings)
    return get_sandbox_status(runtime_settings.sandbox)
