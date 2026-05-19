from fastapi import APIRouter, HTTPException, Query

from app.agent.memory_consolidation import close_session_async
from app.agent.long_term_memory import get_long_term_memory
from app.schemas import (
    MemoryAuditOut,
    MemoryCreate,
    MemoryFileOut,
    MemoryFileSectionOut,
    MemoryFileUpdate,
    MemoryOut,
    MemorySearchOut,
    MemorySectionUpdate,
    MemorySessionCloseIn,
    MemorySessionCloseOut,
    MemoryStatsOut,
    MemoryUpdate,
)

router = APIRouter()


@router.get("/memories", response_model=list[MemoryOut])
async def list_memories(
    query: str = "",
    category: str = "",
    status: str = Query("active", pattern="^(active|archived|)$"),
    review_state: str = Query("", pattern="^(new|reviewed|)$"),
    limit: int = Query(100, ge=1, le=500),
):
    store = get_long_term_memory()
    return store.list_memories(
        query=query,
        category=category,
        status=status,
        review_state=review_state,
        limit=limit,
    )


@router.post("/memories", response_model=MemoryOut)
async def create_memory(body: MemoryCreate):
    store = get_long_term_memory()
    memory = store.remember(
        body.content,
        category=body.category,
        confidence=body.confidence,
        review_state=body.review_state,
        importance=body.importance,
        kind=body.kind,
        source="manual",
    )
    if memory is None:
        raise HTTPException(status_code=400, detail="Memory was rejected by safety filters")
    return memory


@router.get("/memories/search", response_model=list[MemorySearchOut])
async def search_memories(
    query: str = Query(..., min_length=1),
    category: str = "",
    include_archived: bool = False,
    review_state: str = Query("", pattern="^(new|reviewed|)$"),
    limit: int = Query(10, ge=1, le=100),
):
    return get_long_term_memory().score(
        query,
        category=category,
        include_archived=include_archived,
        review_state=review_state,
        limit=limit,
        mark_used=False,
    )


@router.get("/memories/stats", response_model=MemoryStatsOut)
async def memory_stats():
    return get_long_term_memory().stats()


@router.get("/memories/audit", response_model=list[MemoryAuditOut])
async def memory_audit(
    conversation_id: str = "",
    memory_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
):
    return get_long_term_memory().audit_log(
        conversation_id=conversation_id,
        memory_id=memory_id,
        limit=limit,
    )


@router.post("/memories/session/close", response_model=MemorySessionCloseOut)
async def close_memory_session(body: MemorySessionCloseIn):
    return await close_session_async(body.conversation_id)


@router.get("/memories/files/{category}", response_model=MemoryFileOut)
async def get_memory_file(category: str):
    store = get_long_term_memory()
    normalized = store._normalize_category(category)
    if normalized != category:
        raise HTTPException(status_code=400, detail="Invalid memory category")
    return store.memory_file(category)


@router.put("/memories/files/{category}", response_model=MemoryFileOut)
async def save_memory_file(category: str, body: MemoryFileUpdate):
    store = get_long_term_memory()
    normalized = store._normalize_category(category)
    if normalized != category:
        raise HTTPException(status_code=400, detail="Invalid memory category")
    try:
        return store.save_memory_file(category, body.raw_markdown)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/memories/sections/{section_id}", response_model=MemoryFileSectionOut)
async def update_memory_section(section_id: str, body: MemorySectionUpdate):
    try:
        section = get_long_term_memory().update_section(
            section_id,
            **body.model_dump(exclude_unset=True),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if section is None:
        raise HTTPException(status_code=404, detail="Memory section not found")
    return section


@router.get("/memories/{memory_id}", response_model=MemoryOut)
async def get_memory(memory_id: str):
    memory = get_long_term_memory().get(memory_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return memory


@router.patch("/memories/{memory_id}", response_model=MemoryOut)
async def update_memory(memory_id: str, body: MemoryUpdate):
    try:
        memory = get_long_term_memory().update(
            memory_id,
            **body.model_dump(exclude_unset=True),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return memory


@router.delete("/memories/{memory_id}")
async def delete_memory(memory_id: str):
    if not get_long_term_memory().delete(memory_id):
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"ok": True}
