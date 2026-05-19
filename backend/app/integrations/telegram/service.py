from __future__ import annotations

import hashlib
import logging

from app.integrations.telegram.bridge import build_application, register_bot_commands

logger = logging.getLogger(__name__)


def _token_fingerprint(token: str) -> str:
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


class TelegramBotService:
    def __init__(self) -> None:
        self._application = None
        self._token_fingerprint = ""

    @property
    def running(self) -> bool:
        return bool(self._application and getattr(self._application, "running", False))

    async def start(self, token: str = "") -> bool:
        bot_token = token.strip()
        if not bot_token:
            await self.stop()
            return False

        fingerprint = _token_fingerprint(bot_token)
        if self.running and fingerprint == self._token_fingerprint:
            return True

        await self.stop()
        application = build_application(bot_token)
        try:
            await application.initialize()
            await register_bot_commands(application.bot)
            if application.updater is None:
                raise RuntimeError("Telegram application was built without an updater.")
            await application.updater.start_polling(drop_pending_updates=True)
            await application.start()
        except Exception:
            logger.exception("Failed to start Telegram bot service.")
            try:
                await application.shutdown()
            except Exception:
                pass
            self._application = None
            self._token_fingerprint = ""
            return False

        self._application = application
        self._token_fingerprint = fingerprint
        logger.info("Telegram bot service started.")
        return True

    async def restart(self, token: str = "") -> bool:
        await self.stop()
        return await self.start(token)

    async def stop(self) -> None:
        application = self._application
        if application is None:
            self._token_fingerprint = ""
            return

        self._application = None
        self._token_fingerprint = ""
        try:
            if application.updater is not None and getattr(application.updater, "running", False):
                await application.updater.stop()
            if getattr(application, "running", False):
                await application.stop()
            await application.shutdown()
        except Exception:
            logger.exception("Failed to stop Telegram bot service cleanly.")

    def get_bot(self):
        application = self._application
        if application is None:
            return None
        return getattr(application, "bot", None)


telegram_bot_service = TelegramBotService()


async def start_telegram_bot(token: str = "") -> bool:
    return await telegram_bot_service.start(token)


async def restart_telegram_bot(token: str = "") -> bool:
    return await telegram_bot_service.restart(token)


async def stop_telegram_bot() -> None:
    await telegram_bot_service.stop()


def get_telegram_bot():
    return telegram_bot_service.get_bot()
