from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.config import settings
from app.agent.observability.recorder import get_observability_recorder, install_logging_handler
from app.agent.workspace_instructions import ensure_workspace_instruction_file
from app.skills.browser_use.manager import ensure_browser_use_runtime_dirs

logger = logging.getLogger(__name__)
install_logging_handler()


def _split_csv_setting(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_workspace_instruction_file()
    ensure_browser_use_runtime_dirs()
    scheduler_service = None
    app.state.scheduler_status = {"running": False, "startup_error": ""}
    app.state.telegram_status = {"configured": bool(settings.telegram_bot_token), "running": False, "startup_error": ""}
    try:
        from app.agent.scheduler import ScheduledTaskService
        from app.integrations.telegram.agent_bridge import build_runtime_settings

        scheduler_service = ScheduledTaskService(settings_factory=build_runtime_settings)
        await scheduler_service.start()
        app.state.scheduler_status = {"running": True, "startup_error": ""}
    except Exception:
        scheduler_service = None
        app.state.scheduler_status = {"running": False, "startup_error": "scheduled task service failed to start"}
        logger.exception("scheduled-tasks: failed to start")

    telegram_started = False
    if settings.telegram_bot_token:
        try:
            from app.integrations.telegram.service import start_telegram_bot

            telegram_started = await start_telegram_bot(settings.telegram_bot_token)
            if not telegram_started:
                app.state.telegram_status = {
                    "configured": True,
                    "running": False,
                    "startup_error": "telegram bot did not start",
                }
                logger.error("Telegram bot did not start; check token, network, and allowlist settings.")
            else:
                app.state.telegram_status = {"configured": True, "running": True, "startup_error": ""}
        except Exception:
            telegram_started = False
            app.state.telegram_status = {
                "configured": True,
                "running": False,
                "startup_error": "telegram bot startup failed",
            }
            logger.exception("Telegram bot startup failed.")
    try:
        yield
    finally:
        if scheduler_service is not None:
            await scheduler_service.stop()
        if telegram_started:
            try:
                from app.integrations.telegram.service import stop_telegram_bot

                await stop_telegram_bot()
            except Exception:
                logger.exception("Telegram bot shutdown failed.")


app = FastAPI(title="Monaw Agent API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_split_csv_setting(settings.cors_allow_origins),
    allow_origin_regex=settings.cors_allow_origin_regex or None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")


@app.middleware("http")
async def observability_exception_middleware(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as exc:
        get_observability_recorder().log_error(
            message=str(exc),
            logger_name=__name__,
            module="main",
            error_type=type(exc).__name__,
            metadata={
                "method": request.method,
                "path": request.url.path,
            },
        )
        raise


@app.get("/health")
async def health():
    return {"status": "ok", "port": settings.port}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=settings.port, reload=False)
