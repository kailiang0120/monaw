from fastapi import APIRouter

from app.api.routes.access_grants import router as access_grants_router
from app.api.routes.approvals import router as approvals_router
from app.api.routes.chat import router as chat_router
from app.api.routes.conversations import router as conversations_router
from app.api.routes.diagnostics import router as diagnostics_router
from app.api.routes.files import router as files_router
from app.api.routes.memories import router as memories_router
from app.api.routes.observability import router as observability_router
from app.api.routes.sandbox import router as sandbox_router
from app.api.routes.settings import router as settings_router
from app.api.routes.scheduled_tasks import router as scheduled_tasks_router
from app.api.routes.speech_to_text import router as speech_to_text_router
from app.api.routes.uploads import router as uploads_router

router = APIRouter()
router.include_router(chat_router)
router.include_router(conversations_router)
router.include_router(access_grants_router)
router.include_router(settings_router)
router.include_router(memories_router)
router.include_router(approvals_router)
router.include_router(diagnostics_router)
router.include_router(observability_router)
router.include_router(sandbox_router)
router.include_router(files_router)
router.include_router(uploads_router)
router.include_router(speech_to_text_router)
router.include_router(scheduled_tasks_router)
