"""Model catalog for the settings UI: built-in models, the signed-in ChatGPT
account's own list, and models the user added by name."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import httpx

from app.agent.llm_constants import CHAT_MODELS_BY_PROVIDER

logger = logging.getLogger(__name__)

# The account token reads the same catalog the Codex tools use. It is not a
# published API, so any failure falls back to the built-in list.
ACCOUNT_MODELS_URL = "https://api.openai.com/v1/models"
ACCOUNT_MODELS_TTL_SECONDS = 600
# Levels Monaw sends; the account also lists "ultra", which requests map to "max".
_REASONING_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

_account_cache: tuple[float, list["AccountModel"]] | None = None
_account_lock = threading.Lock()


@dataclass(frozen=True)
class AccountModel:
    id: str
    label: str
    reasoning_efforts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProviderModelOptions:
    id: str
    label: str
    models: list[str]
    labels: dict[str, str] = field(default_factory=dict)
    reasoning_efforts: dict[str, list[str]] = field(default_factory=dict)
    source: str = "built_in"


def _provider_label(provider: str) -> str:
    if provider == "gemini":
        return "Google"
    if provider == "codex":
        return "OpenAI account"
    return "OpenAI API"


def parse_account_models(payload: object) -> list[AccountModel]:
    """The models an account offers in its picker, in the account's order."""
    entries = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    listed = [
        entry for entry in entries
        if isinstance(entry, dict) and entry.get("visibility") == "list" and str(entry.get("slug") or "").strip()
    ]
    listed.sort(key=lambda entry: entry.get("priority") if isinstance(entry.get("priority"), (int, float)) else 1_000)
    models = []
    for entry in listed:
        levels = entry.get("supported_reasoning_levels") or []
        efforts = [
            str(level.get("effort") if isinstance(level, dict) else level)
            for level in levels if isinstance(levels, list)
        ]
        models.append(AccountModel(
            id=str(entry["slug"]).strip(),
            label=str(entry.get("display_name") or entry["slug"]).strip(),
            reasoning_efforts=[effort for effort in _REASONING_LEVELS if effort in efforts],
        ))
    return models


def _fetch_account_models() -> list[AccountModel] | None:
    """None when no ChatGPT account is signed in."""
    from app.agent.chatgpt_account import chatgpt_account

    if not chatgpt_account.status().get("connected"):
        return None
    token = chatgpt_account.access_token()
    response = httpx.get(ACCOUNT_MODELS_URL, headers={"Authorization": f"Bearer {token}"}, timeout=10)
    response.raise_for_status()
    return parse_account_models(response.json())


def account_models(*, refresh: bool = False) -> list[AccountModel]:
    """The signed-in account's models, cached briefly; empty when unavailable."""
    global _account_cache
    with _account_lock:
        if not refresh and _account_cache is not None and time.monotonic() - _account_cache[0] < ACCOUNT_MODELS_TTL_SECONDS:
            return _account_cache[1]
    try:
        models = _fetch_account_models()
    except Exception as exc:  # noqa: BLE001 - any failure means the built-in list
        logger.warning("Could not load the ChatGPT account's models: %s", exc)
        models = []
    with _account_lock:
        if models is None:
            _account_cache = None  # signed out
            return []
        # Only a good list is cached, so a sign-in shows at once and an outage
        # keeps the last good list.
        if models:
            _account_cache = (time.monotonic(), models)
        return _account_cache[1] if _account_cache is not None else []


def model_options_payload(custom_models: dict[str, list[str]] | None = None) -> dict:
    custom_models = custom_models or {}
    live = account_models()
    providers = []
    for provider, built_in in CHAT_MODELS_BY_PROVIDER.items():
        if provider == "codex" and live:
            option = ProviderModelOptions(
                id=provider, label=_provider_label(provider), models=[model.id for model in live],
                labels={model.id: model.label for model in live},
                reasoning_efforts={model.id: model.reasoning_efforts for model in live if model.reasoning_efforts},
                source="account",
            )
        else:
            option = ProviderModelOptions(id=provider, label=_provider_label(provider), models=list(built_in))
        extra = [name for name in custom_models.get(provider, []) if name not in option.models]
        providers.append({**option.__dict__, "models": [*option.models, *extra]})
    return {"providers": providers}
