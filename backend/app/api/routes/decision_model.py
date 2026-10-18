from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request

from app.agent.decision_model_server import ManagedLayaServer
from app.agent.observability.recorder import get_observability_recorder
from app.agent.settings_store import load_agent_settings
from app.agent.tool_selection_report import recent_shadow_stats
from app.config import settings
from app.schemas import DecisionModelStatusPayload

router = APIRouter()


def managed_server(request: Request) -> ManagedLayaServer:
    server = getattr(request.app.state, "decision_model_server", None)
    if server is None:
        # The lifespan normally creates it; this keeps the route usable without one.
        server = ManagedLayaServer()
        request.app.state.decision_model_server = server
    return server


@router.get("/decision-model/status", response_model=DecisionModelStatusPayload)
async def decision_model_status(request: Request):
    config = load_agent_settings(settings).decision_model
    payload = await managed_server(request).describe(config)
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    stats = recent_shadow_stats(get_observability_recorder().db_path, since=since)
    return {
        **payload,
        "decisions_24h": stats["decisions"],
        "scored_24h": stats["scored"],
        "covered_24h": stats["covered"],
        "last_decision_at": stats["last_decision_at"],
        "last_status": stats["last_status"],
        "last_top_domain": stats["last_top_domain"],
    }
