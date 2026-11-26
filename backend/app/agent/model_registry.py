"""Connection identities and model metadata shared by settings, UI and runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ProviderId = Literal["openai", "gemini", "codex"]
OptionalProviderId = ProviderId | Literal[""]


@dataclass(frozen=True)
class ModelConnection:
    """A stable connection identity, independent of its API protocol."""

    id: ProviderId
    label: str
    protocol: str
    models: tuple[str, ...]
    api_key_attribute: str = ""

    def api_key(self, settings: Any) -> str:
        """Resolve credentials internally without including them in the catalog."""
        return str(getattr(settings, self.api_key_attribute, "") or "") if self.api_key_attribute else ""

    def readiness(self, settings: Any, *, account_connected: bool = False) -> tuple[bool, str]:
        """Report configured credentials, not network or entitlement availability."""
        ready = bool(self.api_key(settings).strip()) if self.api_key_attribute else account_connected
        reason = "Add an API key in Settings." if self.api_key_attribute else "Sign in to your ChatGPT account in Settings."
        return ready, "" if ready else reason


OPENAI_MODELS = (
    "gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra",
    "gpt-6.2-sol", "gpt-6.2-astra", "gpt-6.1-terra",
)
GEMINI_4_MODELS = ("gemini-4-neon", "gemini-4-hydrogen", "gemini-4-carbon", "gemini-4-argon")
GEMINI_MODELS = (
    "gemini-pro-latest", "gemini-flash-latest", "gemini-flash-lite-latest",
    *GEMINI_4_MODELS,
)
MODEL_LABELS = {
    "gpt-6-luna": "GPT-6 Luna",
    "gpt-6.1-sol": "GPT-6.1 Sol",
    "gpt-6-astra": "GPT-6 Astra",
    "gpt-6.2-sol": "GPT-6.2 Sol",
    "gpt-6.2-astra": "GPT-6.2 Astra",
    "gpt-6.1-terra": "GPT-6.1 Terra",
    "gemini-pro-latest": "Gemini Pro Latest",
    "gemini-flash-latest": "Gemini Flash Latest",
    "gemini-flash-lite-latest": "Gemini Flash Lite Latest",
    "gemini-4-neon": "Gemini 4 Neon",
    "gemini-4-hydrogen": "Gemini 4 Hydrogen",
    "gemini-4-carbon": "Gemini 4 Carbon",
    "gemini-4-argon": "Gemini 4 Argon",
}
CONNECTIONS = {
    "openai": ModelConnection("openai", "OpenAI API", "openai_responses", OPENAI_MODELS, "openai_api_key"),
    "codex": ModelConnection("codex", "OpenAI account", "chatgpt_account", OPENAI_MODELS),
    "gemini": ModelConnection("gemini", "Google", "gemini_generate_content", GEMINI_MODELS, "google_api_key"),
}


def get_connection(connection_id: str) -> ModelConnection:
    """Resolve a registered connection; never route an unknown ID to a default."""
    try:
        return CONNECTIONS[str(connection_id).strip().lower()]
    except KeyError as exc:
        raise ValueError(f"Unsupported model connection: {connection_id!r}.") from exc


def reasoning_efforts(connection_id: str, model_name: str) -> list[str]:
    """Return supported controls for known models; unknown models expose none."""
    connection = get_connection(connection_id)
    model = model_name.lower()
    if connection.protocol == "gemini_generate_content":
        if model in {"gemini-4-neon", "gemini-4-hydrogen"}:
            return ["low", "medium", "high", "xhigh"]
        if model in {"gemini-4-carbon", "gemini-4-argon"}:
            return ["low", "medium", "high"]
        if model in {"gemini-pro-latest", "gemini-flash-latest"}:
            return ["low", "medium", "high"]
        if model == "gemini-flash-lite-latest":
            return ["minimal", "low", "medium", "high"]
        if model == "gemini-2.5-pro":
            return ["low", "medium", "high"]
        if model in {"gemini-2.5-flash", "gemini-2.5-flash-lite"}:
            return ["none", "low", "medium", "high"]
        return []
    if model in OPENAI_MODELS or model == "gpt-6-sol":
        levels = ["none", "low", "medium", "high", "xhigh", "max"]
        return levels if model in {"gpt-6-luna", "gpt-6.1-terra"} else levels[1:]
    return []


def normalize_reasoning_effort(connection_id: str, model_name: str, effort: str) -> str:
    """Normalize known controls while retaining explicit choices for unknown models."""
    effort = "max" if effort == "ultra" else effort
    levels = reasoning_efforts(connection_id, model_name)
    if not levels or effort in levels:
        return effort
    if effort == "minimal" and "low" in levels:
        return "low"  # A legacy minimum must not turn reasoning off.
    if effort in {"xhigh", "max"} and "high" in levels:
        return levels[-1]  # Retain the highest supported level without disabling thinking.
    if effort in {"none", "minimal"}:
        return levels[0]
    return "medium" if "medium" in levels else levels[0]


def initialize_provider_client(client: Any) -> None:
    """Initialize the SDK chosen by the connection's protocol."""
    protocol = get_connection(client.provider).protocol
    if protocol == "gemini_generate_content":
        from google import genai

        client._genai_client = genai.Client(api_key=client.api_key) if client.api_key else None
    elif protocol == "openai_responses":
        from openai import AsyncOpenAI

        client._openai_client = AsyncOpenAI(api_key=client.api_key, max_retries=0)
    elif protocol == "chatgpt_account":
        client._chatgpt_token = ""
    else:
        raise ValueError(f"Unsupported model protocol: {protocol!r}.")
