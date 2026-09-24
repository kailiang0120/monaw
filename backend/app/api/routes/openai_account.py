"""ChatGPT account sign-in for the Codex SDK provider."""

from fastapi import APIRouter, HTTPException

from app.agent.codex_account import codex_account

router = APIRouter(prefix="/openai-account", tags=["openai-account"])


@router.get("/status")
async def account_status() -> dict:
    try:
        return await codex_account.status()
    except Exception as exc:
        raise HTTPException(503, detail="OpenAI account status unavailable.") from exc


@router.post("/login")
async def account_login() -> dict:
    try:
        return await codex_account.start_login()
    except Exception as exc:
        raise HTTPException(503, detail="OpenAI account sign-in unavailable.") from exc


@router.post("/logout")
async def account_logout() -> dict:
    try:
        await codex_account.logout()
        return {"connected": False, "plan": "", "limits": [], "usage_error": ""}
    except Exception as exc:
        raise HTTPException(503, detail="OpenAI account sign-out unavailable.") from exc
