from fastapi import APIRouter, HTTPException, Query

from app.agent.memory_store import MemoryWriteError, get_memory_store
from app.agent.ui_events import publish_ui_event
from app.schemas import (
    MemoryCreate,
    MemoryOut,
    MemorySearchOut,
    MemoryUpdate,
    MemoryWorkspaceOut,
    OkResponse,
)

router = APIRouter()

# ``workspace`` is a folder path; empty means Monaw's default workspace. Global
# memories are always included alongside the workspace's own.


@router.get("/memories", response_model=list[MemoryOut])
async def list_memories(
    workspace: str = "",
    scope: str = Query("", pattern="^(global|workspace|)$"),
):
    return [entry.public() for entry in get_memory_store().entries(workspace, scope=scope)]


@router.get("/memories/search", response_model=list[MemorySearchOut])
async def search_memories(
    query: str = Query(..., min_length=1),
    workspace: str = "",
    scope: str = Query("", pattern="^(global|workspace|)$"),
    limit: int = Query(10, ge=1, le=100),
):
    return get_memory_store().search(query, workspace=workspace, scope=scope, limit=limit)


@router.get("/memories/workspaces", response_model=list[MemoryWorkspaceOut])
async def list_memory_workspaces():
    return get_memory_store().list_workspaces()


@router.post("/memories", response_model=MemoryOut)
async def create_memory(body: MemoryCreate):
    try:
        entry = get_memory_store().add(
            body.content,
            scope=body.scope,
            kind=body.kind,
            importance=body.importance,
            workspace=body.workspace,
            source="manual",
        )
    except MemoryWriteError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    publish_ui_event("memory.changed", {"memory_id": entry.id, "action": "created"})
    return entry.public()


@router.patch("/memories/{memory_id}", response_model=MemoryOut)
async def update_memory(memory_id: str, body: MemoryUpdate, workspace: str = ""):
    try:
        entry = get_memory_store().update(memory_id, workspace=workspace, **body.model_dump(exclude_unset=True))
    except MemoryWriteError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if entry is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    publish_ui_event("memory.changed", {"memory_id": memory_id, "action": "updated"})
    return entry.public()


@router.delete("/memories/{memory_id}", response_model=OkResponse)
async def delete_memory(memory_id: str, workspace: str = ""):
    if not get_memory_store().delete(memory_id, workspace=workspace):
        raise HTTPException(status_code=404, detail="Memory not found")
    publish_ui_event("memory.changed", {"memory_id": memory_id, "action": "deleted"})
    return {"ok": True}
