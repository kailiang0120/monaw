import asyncio

from fastapi import APIRouter, HTTPException

from app.agent.storage_usage import StorageCleanupError, clean_storage, storage_usage
from app.schemas import StorageCleanResult, StorageUsage

router = APIRouter()


@router.get("/settings/storage", response_model=StorageUsage)
async def get_storage_usage():
    # Walking the Monaw folders can take a moment; keep the event loop free.
    return await asyncio.to_thread(storage_usage)


@router.post("/settings/storage/{category}/clean", response_model=StorageCleanResult)
async def clean_storage_category(category: str):
    try:
        return await asyncio.to_thread(clean_storage, category)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="This storage category cannot be cleaned here.") from exc
    except StorageCleanupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
