"""Model catalog for the settings UI: built-in models, the signed-in ChatGPT
account's own list, and models the user added by name."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import httpx

from app.agent.model_registry import CONNECTIONS, MODEL_LABELS, reasoning_efforts

logger = logging.getLogger(__name__)

# The account token reads the same catalog the Codex tools use. It is not a
# published API, so any failure falls back to the built-in list.
ACCOUNT_MODELS_URL = "https://api.openai.com/v1/models"
ACCOUNT_MODELS_TTL_SECONDS = 600
# Levels Monaw sends; the account also lists "ultra", which requests map to "max".
_REASONING_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

_account_cache: tuple[float, list["AccountModel"]] | None = None
_account_cache_identity = ""
_account_lock = threading.Lock()


@dataclass(frozen=True)
class AccountModel:
    id: str
    label: str
    reasoning_efforts: list[str] = field(default_factory=list)


def _account_identity() -> str:
    """Read the signed-in identity before consulting account-specific metadata."""
    from app.agent.chatgpt_account import chatgpt_account

    return chatgpt_account.catalog_identity()


def parse_account_models(payload: object) -> list[AccountModel]:
    """The models an account offers in its picker, in the account's order."""
    entries = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(entries, list) and isinstance(payload, dict) and isinstance(payload.get("data"), list):
        # Standard /v1/models also lists embeddings/audio/image models. Only
        # known chat IDs are eligible; no reasoning contract is implied.
        chat_ids = set(CONNECTIONS["codex"].models)
        return [AccountModel(id=entry["id"].strip(), label=MODEL_LABELS.get(entry["id"].strip(), entry["id"].strip()))
                for entry in payload["data"] if isinstance(entry, dict)
                and isinstance(entry.get("id"), str) and entry["id"].strip() in chat_ids]
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
    global _account_cache, _account_cache_identity
    identity = _account_identity()
    with _account_lock:
        if identity != _account_cache_identity or not identity:
            _account_cache = None
            _account_cache_identity = identity
        if not identity:
            return []
        if not refresh and _account_cache is not None and time.monotonic() - _account_cache[0] < ACCOUNT_MODELS_TTL_SECONDS:
            return _account_cache[1]
    try:
        models = _fetch_account_models()
    except Exception as exc:  # noqa: BLE001 - any failure means the built-in list
        logger.warning("Could not load the ChatGPT account's models: %s", exc)
        models = []
    with _account_lock:
        if identity != _account_identity():
            return []  # A completed request from an old account cannot refill the cache.
        if models is None:
            _account_cache = None  # signed out
            return []
        # Only a good list is cached, so a sign-in shows at once and an outage
        # keeps the last good list.
        if models:
            _account_cache = (time.monotonic(), models)
        return _account_cache[1] if _account_cache is not None else []


def model_options_payload(custom_models: dict[str, list[str]] | None = None, *, settings=None) -> dict:
    """Publish one secret-free registry for every model selection surface."""
    from app.agent.chatgpt_account import chatgpt_account

    custom_models = custom_models or {}
    live = account_models()
    account = chatgpt_account.status()
    providers = []
    for provider, connection in CONNECTIONS.items():
        models = list(connection.models)
        labels = {model: MODEL_LABELS.get(model, model) for model in models}
        efforts = {model: reasoning_efforts(provider, model) for model in models}
        source = "built_in"
        if provider == "codex" and live:
            models = list(dict.fromkeys(model.id for model in live))
            labels = {model.id: model.label for model in live}
            efforts = {model.id: model.reasoning_efforts for model in live}
            source = "account"
        for model in custom_models.get(provider, []):
            if model not in models:
                models.append(model)
                labels[model] = MODEL_LABELS.get(model, model)
                efforts[model] = reasoning_efforts(provider, model)
        available, reason = connection.readiness(
            settings, account_connected=bool(account.get("connected") and account.get("plan_usage")),
        )
        providers.append({
            "id": provider, "connection_id": connection.id, "label": connection.label,
            "protocol": connection.protocol, "available": available, "unavailable_reason": reason,
            "models": models, "labels": labels, "reasoning_efforts": efforts, "source": source,
        })
    return {"providers": providers}
