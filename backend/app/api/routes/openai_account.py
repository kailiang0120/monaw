"""Sign in with ChatGPT, so model calls can use the account's plan."""

import asyncio
import logging

from fastapi import APIRouter, HTTPException

from app.agent.chatgpt_account import chatgpt_account

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/openai-account", tags=["openai-account"])


@router.get("/status")
async def account_status() -> dict:
    try:
        return chatgpt_account.status()
    except Exception as exc:
        # The response stays generic; the log keeps the cause.
        logger.exception("OpenAI account status failed")
        raise HTTPException(503, detail="OpenAI account status unavailable.") from exc


@router.post("/login")
async def account_login() -> dict:
    try:
        return await asyncio.to_thread(chatgpt_account.start_login)
    except Exception as exc:
        logger.exception("OpenAI account sign-in failed")
        raise HTTPException(503, detail="OpenAI account sign-in unavailable.") from exc


@router.post("/logout")
async def account_logout() -> dict:
    try:
        return await asyncio.to_thread(chatgpt_account.logout)
    except Exception as exc:
        logger.exception("OpenAI account sign-out failed")
        raise HTTPException(503, detail="OpenAI account sign-out unavailable.") from exc
