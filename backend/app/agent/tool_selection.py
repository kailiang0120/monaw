"""System 1 tool-selection decisions from an external decision model (Laya).

The selector asks a decision model which skill domains a user turn needs.
In ``shadow`` mode the answer is only recorded: the turn loop still sends
every visible tool to the LLM, then logs the decision next to the domains of
the tools the LLM actually called so the selection policy can be tuned
before it is ever allowed to narrow the tool list.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

import httpx

from app.agent.decision_model_server import managed_laya_executable

logger = logging.getLogger(__name__)

TOOL_SEARCH_DOMAIN = "tool-search"
BUILTIN_DOMAIN = "builtin"
QUESTION_ID = "capability"
# There is deliberately no "no tool needed" option: Laya over-picks it and
# confidently drops domains the request needs. Keeping a domain the turn did
# not use only costs prompt tokens.
QUESTION_INSTRUCTIONS = "Which capability does the assistant need to complete the latest request?"

# Laya rejects more than 100 options on one choice question (HTTP 413).
MAX_OPTIONS = 100
_MAX_REQUEST_CHARS = 2000
_MAX_HISTORY_CHARS = 400
_FOLLOW_UP_MAX_WORDS = 6
_MAX_OPTION_CHARS = 120
_FAILURE_COOLDOWN_SECONDS = 30.0
_PSEUDO_TOOLS = frozenset({"final_answer"})


class DecisionModelError(RuntimeError):
    """The decision model could not produce a usable answer."""

    def __init__(self, message: str, *, unavailable: bool = False) -> None:
        super().__init__(message)
        self.unavailable = unavailable


def tool_domain(tool: dict | None) -> str:
    """Return the selectable domain a registered tool belongs to."""
    if not tool:
        return ""
    bridge = tool.get("mcp_bridge") or {}
    if bridge.get("skill"):
        return str(bridge["skill"])
    server = str(bridge.get("server_name") or "").strip()
    if server:
        return f"mcp:{server}"
    skill = str((tool.get("metadata") or {}).get("skill") or "").strip()
    if skill:
        return skill
    if tool.get("name") == "tool_search":
        return TOOL_SEARCH_DOMAIN
    return BUILTIN_DOMAIN


def _clip(text: Any, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def _is_loopback_endpoint(endpoint: str) -> bool:
    host = (urlparse(endpoint).hostname or "").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class LayaClient:
    """Minimal async client for ``laya-serve``'s ``POST /v1/systemone``."""

    def __init__(
        self,
        endpoint: str,
        *,
        api_key: str = "",
        timeout_seconds: float = 3.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._transport = transport
        # A system proxy must not intercept a decision model on this machine.
        self._trust_env = not _is_loopback_endpoint(self.endpoint)

    async def predict(self, state: Any, questions: dict[str, dict], *, model: str = "") -> dict:
        body: dict[str, Any] = {"state": state, "questions": questions}
        if model:
            body["model"] = model
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self._transport,
                trust_env=self._trust_env,
            ) as client:
                response = await client.post(f"{self.endpoint}/v1/systemone", json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise DecisionModelError(f"timeout after {self.timeout_seconds:g}s", unavailable=True) from exc
        except httpx.HTTPError as exc:
            raise DecisionModelError(f"{type(exc).__name__}: {exc}", unavailable=True) from exc

        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", "")
            except (ValueError, AttributeError):
                detail = response.text
            raise DecisionModelError(
                f"HTTP {response.status_code}: {_clip(detail, 200)}",
                unavailable=response.status_code in {401, 403, 404} or response.status_code >= 500,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise DecisionModelError("response is not JSON") from exc
        if not isinstance(payload, dict):
            raise DecisionModelError("response is not a JSON object")
        return payload


@dataclass(frozen=True, slots=True)
class DomainOption:
    domain: str
    description: str
    tool_names: tuple[str, ...] = ()


@dataclass(slots=True)
class ToolSelectionDecision:
    """One decision-model answer, plus the domains the policy would keep.

    ``full_fallback`` means the policy would send every visible tool, either
    because the model was unsure or because it gave no usable answer.
    """

    status: str
    mode: str = "shadow"
    model: str = ""
    latency_ms: int = 0
    options: list[str] = field(default_factory=list)
    probabilities: dict[str, float] = field(default_factory=dict)
    selected_domains: list[str] = field(default_factory=list)
    pinned_domains: list[str] = field(default_factory=list)
    top_domain: str = ""
    top_probability: float = 0.0
    confidence: float | None = None
    full_fallback: bool = True
    error: str = ""

    def kept_domains(self, visible_domains: Iterable[str]) -> set[str]:
        visible = set(visible_domains)
        if self.full_fallback:
            return visible
        return (set(self.selected_domains) | set(self.pinned_domains)) & visible

    def as_metadata(self) -> dict[str, Any]:
        return asdict(self)


def select_domains(probabilities: dict[str, float], *, coverage: float, max_domains: int) -> list[str]:
    """Smallest set of top options whose probability mass reaches ``coverage``."""
    ranked = sorted(probabilities.items(), key=lambda item: (-item[1], item[0]))
    selected: list[str] = []
    mass = 0.0
    for domain, probability in ranked:
        # The epsilon keeps float rounding (0.6 + 0.3 < 0.9) from adding an option.
        if len(selected) >= max_domains or mass >= coverage - 1e-9:
            break
        selected.append(domain)
        mass += probability
    return selected


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") in {"text", "input_text", "output_text"}
        ]
        return " ".join(part for part in parts if part)
    return ""


def _last_text(history: list[dict], role: str) -> str:
    for message in reversed(history):
        if message.get("role") == role and not message.get("tool_calls"):
            text = _message_text(message).strip()
            if text:
                return text
    return ""


class ToolSelector:
    """Scores which skill domains a turn needs with a Laya decision model."""

    def __init__(
        self,
        config: Any,
        *,
        skill_summaries: dict[str, str] | None = None,
        mcp_descriptions: dict[str, str] | None = None,
        client: LayaClient | None = None,
        api_key: str = "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.mode = str(getattr(config, "mode", "shadow") or "shadow")
        self.model = str(getattr(config, "model", "") or "")
        self.timeout_seconds = max(0.1, float(getattr(config, "timeout_ms", 3000) or 3000) / 1000.0)
        self.coverage = float(getattr(config, "coverage", 0.9))
        self.max_domains = max(1, int(getattr(config, "max_domains", 4)))
        self.min_confidence = float(getattr(config, "min_confidence", 0.4))
        self.pinned_domains = sorted({str(item).strip() for item in getattr(config, "pinned_domains", []) or [] if str(item).strip()})
        self.skill_summaries = dict(skill_summaries or {})
        self.mcp_descriptions = dict(mcp_descriptions or {})
        self.client = client or LayaClient(
            str(getattr(config, "endpoint", "") or ""),
            api_key=api_key,
            timeout_seconds=self.timeout_seconds,
        )
        self._clock = clock
        self._unavailable_until = 0.0

    def build_options(self, visible_tools: list[dict]) -> list[DomainOption]:
        """One option per unpinned domain among the visible tools."""
        tools_by_domain: dict[str, list[str]] = {}
        for tool in visible_tools:
            domain = tool_domain(tool)
            if not domain or domain in self.pinned_domains:
                continue
            tools_by_domain.setdefault(domain, []).append(str(tool.get("name") or ""))

        options: list[DomainOption] = []
        for domain in sorted(tools_by_domain):
            names = tuple(sorted(name for name in tools_by_domain[domain] if name))
            options.append(DomainOption(domain, self._describe(domain, names), names))
        return options

    def _describe(self, domain: str, tool_names: tuple[str, ...]) -> str:
        if domain.startswith("mcp:"):
            server = domain[4:]
            description = self.mcp_descriptions.get(server, "")
            if not description:
                description = f"Tools from the {server} MCP server: {', '.join(tool_names[:6])}"
            return _clip(description, _MAX_OPTION_CHARS)
        summary = self.skill_summaries.get(domain, "")
        if not summary:
            summary = f"Tools: {', '.join(tool_names[:6])}"
        return _clip(summary, _MAX_OPTION_CHARS)

    @staticmethod
    def build_state(user_request: str, history: list[dict] | None = None, *, attachment_count: int = 0) -> dict:
        state: dict[str, Any] = {"latest_request": _clip(user_request, _MAX_REQUEST_CHARS)}
        # The previous turn only helps short follow-ups ("yes, do it"). For a full
        # request it pulls the choice back toward the last topic.
        if len(str(user_request or "").split()) > _FOLLOW_UP_MAX_WORDS:
            history = []
        history = history or []
        previous_user = _last_text(history, "user")
        previous_reply = _last_text(history, "assistant")
        if previous_user:
            state["previous_user_message"] = _clip(previous_user, _MAX_HISTORY_CHARS)
        if previous_reply:
            state["previous_assistant_reply"] = _clip(previous_reply, _MAX_HISTORY_CHARS)
        if attachment_count:
            state["attachment_count"] = attachment_count
        return state

    def _decision(self, status: str, **kwargs) -> ToolSelectionDecision:
        return ToolSelectionDecision(
            status=status,
            mode=self.mode,
            pinned_domains=list(self.pinned_domains),
            **kwargs,
        )

    def timeout_decision(self) -> ToolSelectionDecision:
        return self._decision("timeout", error="decision not ready before the turn ended")

    async def decide(
        self,
        user_request: str,
        *,
        visible_tools: list[dict],
        history: list[dict] | None = None,
        attachment_count: int = 0,
    ) -> ToolSelectionDecision:
        """Ask the decision model which domains this turn needs. Never raises."""
        options = self.build_options(visible_tools)
        labels = [option.domain for option in options]
        if not str(user_request or "").strip():
            return self._decision("skipped", options=labels, error="empty request")
        if len(options) < 2:
            return self._decision("skipped", options=labels, error="fewer than two selectable domains")
        if len(options) > MAX_OPTIONS:
            return self._decision("skipped", options=labels, error=f"{len(options)} options exceeds {MAX_OPTIONS}")
        now = self._clock()
        if now < self._unavailable_until:
            return self._decision("unavailable", options=labels, error="cooling down after a failed call")

        question = {
            "type": "choice",
            "instructions": QUESTION_INSTRUCTIONS,
            "criteria": {option.domain: option.description for option in options},
        }
        started = self._clock()
        try:
            payload = await self.client.predict(
                self.build_state(user_request, history, attachment_count=attachment_count),
                {QUESTION_ID: question},
                model=self.model,
            )
            probabilities, confidence = _parse_choice(payload, labels)
        except DecisionModelError as exc:
            if exc.unavailable:
                self._unavailable_until = self._clock() + _FAILURE_COOLDOWN_SECONDS
            logger.warning("decision model call failed: %s", exc)
            return self._decision(
                "error",
                options=labels,
                latency_ms=round((self._clock() - started) * 1000),
                error=str(exc),
            )
        except Exception as exc:  # pragma: no cover - defensive: shadow mode must never break a turn
            logger.exception("decision model call failed unexpectedly")
            return self._decision("error", options=labels, error=f"{type(exc).__name__}: {exc}")

        latency_ms = round((self._clock() - started) * 1000)
        top_domain, top_probability = max(probabilities.items(), key=lambda item: (item[1], item[0]))
        confident = top_probability >= self.min_confidence
        return self._decision(
            "ok" if confident else "low_confidence",
            model=str(payload.get("model") or (payload.get("routing") or {}).get("model") or self.model),
            latency_ms=latency_ms,
            options=labels,
            probabilities={label: round(value, 4) for label, value in probabilities.items()},
            selected_domains=select_domains(probabilities, coverage=self.coverage, max_domains=self.max_domains),
            top_domain=top_domain,
            top_probability=round(top_probability, 4),
            confidence=confidence,
            full_fallback=not confident,
        )


def _parse_choice(payload: dict, labels: list[str]) -> tuple[dict[str, float], float | None]:
    answer = (payload.get("answers") or {}).get(QUESTION_ID)
    if not isinstance(answer, dict):
        raise DecisionModelError(f"response has no '{QUESTION_ID}' answer")
    raw = answer.get("probabilities")
    if not isinstance(raw, dict):
        raise DecisionModelError("choice answer has no probabilities")
    probabilities: dict[str, float] = {}
    for label in labels:
        try:
            probabilities[label] = max(0.0, float(raw.get(label, 0.0) or 0.0))
        except (TypeError, ValueError):
            probabilities[label] = 0.0
    total = sum(probabilities.values())
    if total <= 0:
        raise DecisionModelError("choice probabilities are all zero")
    probabilities = {label: value / total for label, value in probabilities.items()}
    confidence = answer.get("confidence")
    try:
        confidence = round(float(confidence), 4) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    return probabilities, confidence


def evaluate_selection(
    decision: ToolSelectionDecision,
    *,
    visible_tools: list[dict],
    used_tool_names: Iterable[str],
    lookup_tool: Callable[[str], dict | None],
    all_tools: list[dict] | None = None,
) -> dict[str, Any]:
    """Compare a decision with the tools the LLM actually called this turn.

    ``all_tools`` adds tools hidden until tool search loads them, so a domain the
    model picked still counts as kept when its tools start out hidden.
    """
    known_domains = {tool_domain(tool) for tool in (all_tools if all_tools is not None else visible_tools)}
    known_domains.discard("")
    kept = decision.kept_domains(known_domains)
    used_domains: set[str] = set()
    for name in used_tool_names:
        if not name or name in _PSEUDO_TOOLS:
            continue
        domain = tool_domain(lookup_tool(name)) or BUILTIN_DOMAIN
        used_domains.add(domain)
    missed = sorted(used_domains - kept)
    domain_tool_counts: dict[str, int] = {}
    for tool in visible_tools:
        domain = tool_domain(tool)
        domain_tool_counts[domain] = domain_tool_counts.get(domain, 0) + 1
    kept_tools = [tool for tool in visible_tools if tool_domain(tool) in kept]
    dropped_tools = [tool for tool in visible_tools if tool_domain(tool) not in kept]
    decided = decision.status in {"ok", "low_confidence"}
    metadata = decision.as_metadata()
    metadata.update(
        {
            "kept_domains": sorted(kept),
            "used_domains": sorted(used_domains),
            "missed_domains": missed,
            # None when the model gave no answer, so failures do not count as hits.
            "covered": (not missed) if decided else None,
            "top_domain_used": (decision.top_domain in used_domains) if decided else None,
            "domain_tool_counts": dict(sorted(domain_tool_counts.items())),
            "visible_tool_count": len(visible_tools),
            "kept_tool_count": len(kept_tools),
            "dropped_schema_chars": sum(_schema_chars(tool) for tool in dropped_tools),
        }
    )
    return metadata


def _schema_chars(tool: dict) -> int:
    try:
        parameters = json.dumps(tool.get("parameters") or {}, ensure_ascii=False)
    except (TypeError, ValueError):
        parameters = ""
    return len(str(tool.get("name") or "")) + len(str(tool.get("description") or "")) + len(parameters)


def build_tool_selector(settings: Any, skills: Iterable[Any] = ()) -> ToolSelector | None:
    """Create the configured selector, or ``None`` when it is switched off."""
    config = getattr(settings, "decision_model", None)
    if config is None or not getattr(config, "enabled", False):
        return None
    if str(getattr(config, "provider", "laya")) != "laya":
        logger.warning("decision model provider %r is not supported", getattr(config, "provider", ""))
        return None
    endpoint = str(getattr(config, "endpoint", "") or "").strip()
    if not endpoint.startswith(("http://", "https://")):
        logger.warning("decision model endpoint %r is not an http(s) URL; selector disabled", endpoint)
        return None
    # A managed loopback server only exists once setup.bat has installed Laya.
    if getattr(config, "managed", False) and _is_loopback_endpoint(endpoint) and managed_laya_executable() is None:
        return None

    skill_summaries = {
        str(getattr(skill, "name", "")): str(getattr(skill, "summary", "") or getattr(skill, "description", ""))
        for skill in skills
    }
    mcp_servers = getattr(getattr(settings, "mcp", None), "servers", []) or []
    mcp_descriptions = {
        str(server.get("name") or ""): str(server.get("description") or "")
        for server in mcp_servers
        if isinstance(server, dict)
    }
    return ToolSelector(
        config,
        skill_summaries=skill_summaries,
        mcp_descriptions=mcp_descriptions,
        api_key=str(getattr(settings, "laya_api_key", "") or ""),
    )
