from fastapi import APIRouter, HTTPException

from app.agent.data_lifecycle import delete_runtime_data
from app.schemas import DataDeletionRequest, DataDeletionResult

router = APIRouter()


@router.post("/privacy/delete-data", response_model=DataDeletionResult)
async def privacy_delete_data(payload: DataDeletionRequest):
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Deletion confirmation is required")
    return delete_runtime_data()
