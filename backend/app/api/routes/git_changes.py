"""Profile-scoped Git changes and explicit, file-scoped Undo confirmation."""
import asyncio
import secrets
import time

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from app.agent import git_changes as tracking
from app.api.routes.file_mutations import _authorized_run_context, _runtime_context, _raise

router = APIRouter(prefix="/runs")
_confirmations: dict[str, dict] = {}


class ChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=4096)
    confirmation_id: str = ""


def _scope(request: Request, run_id: str, conversation_id: str):
    return _authorized_run_context(request, run_id=run_id, conversation_id=conversation_id,
                                   expected_revision=None, require_active=False)


def _stopped(scope):
    if scope["snapshot"]["status"] not in {"completed", "failed", "cancelled"}:
        _raise(409, "run_active", "Finish or stop the run before Undo.")
    active = scope["db"].fetchone(
        "SELECT run_id FROM agent_runs WHERE workspace_dir = ? AND status IN ('running', 'pausing', 'waiting_approval') LIMIT 1",
        (scope["scope"]["workspace_dir"],),
    )
    if active:
        _raise(409, "workspace_active", "Another run is using this workspace. Wait for it to finish.")


@router.get("/{run_id}/changes")
async def list_changes(run_id: str, conversation_id: str, request: Request, path: str | None = None):
    scope = _scope(request, run_id, conversation_id)
    def read():
        with _runtime_context(scope):
            try:
                return tracking.changes(scope["workspace_dir"], run_id, name=path)
            except tracking.ChangesError as exc:
                return {"files": [], "source": "git", "run_id": run_id, "unavailable": str(exc), "excluded_count": 0}
    return await asyncio.to_thread(read)


@router.post("/{run_id}/changes/undo-preview")
async def preview_undo(run_id: str, body: ChangeRequest, request: Request):
    scope = _scope(request, run_id, body.conversation_id)
    _stopped(scope)
    def preview():
        with _runtime_context(scope):
            return tracking.changes(scope["workspace_dir"], run_id, name=body.path, inverse=True)
    try:
        result = await asyncio.to_thread(preview)
    except tracking.ChangesError as exc:
        _raise(409, "undo_unavailable", str(exc))
    if not result["files"] or result["files"][0]["undo"] != "available":
        _raise(409, "undo_unavailable", "This change cannot currently be undone. Refresh the Changes panel.")
    for key, record in list(_confirmations.items()):
        if record["expires"] < time.monotonic():
            _confirmations.pop(key, None)
    if len(_confirmations) >= 256:
        _raise(429, "preview_limit", "Too many open Undo previews. Try again shortly.")
    token = secrets.token_urlsafe(32)
    _confirmations[token] = {"run_id": run_id, "path": body.path, "session": scope["session_id"],
                             "revision": scope["snapshot"]["revision"], "workspace": scope["workspace_dir"],
                             "expires": time.monotonic() + 300}
    return {"confirmation_id": token, "file": result["files"][0]}


@router.post("/{run_id}/changes/undo")
async def apply_undo(run_id: str, body: ChangeRequest, request: Request):
    scope = _scope(request, run_id, body.conversation_id)
    _stopped(scope)
    record = _confirmations.get(body.confirmation_id)
    if not record or record != {"run_id": run_id, "path": body.path, "session": scope["session_id"],
                                "revision": scope["snapshot"]["revision"], "workspace": scope["workspace_dir"],
                                "expires": record["expires"]} or record["expires"] < time.monotonic():
        _raise(409, "invalid_confirmation", "The Undo preview expired or no longer matches this file.")
    _confirmations.pop(body.confirmation_id)
    def apply():
        # Fence new Run claims/resumes while applying the exact inverse. Keep
        # lock order consistent with checkpoint reclamation (Git, then DB).
        with tracking.LOCK, scope["db"].transaction(), _runtime_context(scope):
            _stopped(scope)
            tracking.undo(scope["workspace_dir"], run_id, body.path)
    try:
        await asyncio.to_thread(apply)
    except Exception as exc:
        _raise(409, "undo_conflict", str(exc) if isinstance(exc, tracking.ChangesError)
               else "Undo could not complete safely. Refresh and review the file.")
    return {"status": "undone"}
