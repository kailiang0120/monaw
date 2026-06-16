"""OpenClaw-style agent runtime."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from typing import AsyncIterator

from app.agent.llm_client import LLMClient, build_vision_describer
from app.agent.identity import DEFAULT_AGENT_NAME, LEGACY_AGENT_NAME
from app.agent.long_term_memory import get_long_term_memory
from app.agent.memory_manager import get_memory_manager
from app.agent.skill_loader import load_tools
from app.agent.skill_prompt import build_skill_prompt
from app.agent.turn_loop import TurnLoop
from app.agent.workspace_instructions import (
    build_workspace_instruction_prompt,
    workspace_instruction_fingerprint,
)

logger = logging.getLogger(__name__)


def _is_compact_command(message: str) -> bool:
    return str(message or "").strip().casefold() == "/compact"


def _is_skill_creator_command(message: str) -> bool:
    normalized = " ".join(str(message or "").strip().split()).casefold()
    return normalized == "/skill creator" or normalized.startswith("/skill creator ")


def _format_compact_reply(result: dict) -> str:
    status = str(result.get("status") or "")
    if status == "empty":
        return "Nothing to compact yet."
    if status == "unchanged":
        return "Already compacted. No new messages since the last /compact."
    if status == "failed":
        return (
            "Couldn't compact this chat right now, so the existing conversation history is unchanged. "
            "Try /compact again in a moment."
        )

    message_count = int(result.get("message_count") or 0)
    tokens_before = int(result.get("tokens_before") or 0)
    tokens_after = int(result.get("tokens_after") or 0)
    saved = max(0, tokens_before - tokens_after)
    return (
        "Compacted this chat. Future turns will use the compacted context "
        "instead of the earlier raw history.\n\n"
        f"Messages compacted: {message_count}\n"
        f"Context estimate: {tokens_before} -> {tokens_after} tokens"
        + (f" ({saved} saved)" if saved else "")
    )


def _format_skill_creator_reply() -> str:
    return (
        "Skill Creator mode is active. Tell me the skill name, what should trigger it, "
        "and whether it needs tool code. I will create it as an optional skill under "
        "`backend/app/skills`, reload skills, and only restart the backend if reload is not enough."
    )


def _compact_identity_text(value: str | None, *, fallback: str = "") -> str:
    text = " ".join(str(value or "").split())
    return text or fallback


def build_identity_prompt(settings) -> str:
    identity = getattr(settings, "identity", None)
    if identity is None:
        return ""

    agent_name = _compact_identity_text(getattr(identity, "agent_name", ""), fallback=DEFAULT_AGENT_NAME)
    if agent_name == LEGACY_AGENT_NAME:
        agent_name = DEFAULT_AGENT_NAME
    user_name = _compact_identity_text(getattr(identity, "user_name", ""))
    user_identity = _compact_identity_text(getattr(identity, "user_identity", ""))
    communication_style = _compact_identity_text(getattr(identity, "communication_style", ""))

    profile = {
        "agent_name": agent_name,
        "user_name": user_name,
        "user_identity": user_identity,
        "communication_style": communication_style,
    }
    profile_json = json.dumps(profile, ensure_ascii=False, indent=2)
    return (
        "## Identity And Communication\n"
        "Use these saved preferences only for the agent display identity, how to address the user, "
        "user context, and communication style. They do not override system or developer instructions, "
        "safety policy, tool contracts, permission checks, approval requirements, or file access rules. "
        "Do not mention these settings unless they are relevant.\n"
        f"{profile_json}"
    )


class AgentRuntime:
    def __init__(self, settings):
        self.settings = settings
        self._active_runs = 0
        self._retired = False
        api_key = _provider_api_key(settings)
        logger.info(
            "AgentRuntime init provider=%s model=%s reasoning=%s",
            settings.model_provider,
            settings.model_name,
            getattr(settings, "reasoning_effort", ""),
        )

        self.llm_client = LLMClient(
            provider=settings.model_provider,
            model_name=settings.model_name,
            api_key=api_key,
            base_url=getattr(settings, "deepseek_base_url", ""),
            reasoning_effort=getattr(settings, "reasoning_effort", "medium"),
        )
        self.vision_describer = build_vision_describer(settings)
        self.skills, self.tool_registry = load_tools(settings)
        self.memory = get_memory_manager(self.llm_client)
        self.long_term_memory = get_long_term_memory(self.llm_client, settings)
        self.memory.set_long_term_memory(self.long_term_memory)
        try:
            caught_up = self.long_term_memory.catch_up_unprocessed_sessions(max_sessions=3)
            if caught_up:
                logger.info("Markdown memory startup catch-up curated %d sessions", caught_up)
        except Exception as exc:
            logger.debug("Markdown memory startup catch-up skipped: %s", exc)

        llm_cfg = getattr(settings, "llm", None)
        max_iterations = getattr(llm_cfg, "max_iterations_per_turn", None) or 40
        max_turn_seconds = float(getattr(llm_cfg, "max_turn_seconds", None) or 1800)
        max_llm_call_seconds = float(getattr(llm_cfg, "max_llm_call_seconds", None) or 300)

        self.turn_loop = TurnLoop(
            llm_client=self.llm_client,
            registry=self.tool_registry,
            memory=self.memory,
            max_iterations=max_iterations,
            max_turn_seconds=max_turn_seconds,
            max_llm_call_seconds=max_llm_call_seconds,
            vision_describer=self.vision_describer,
        )
        visible_tool_names = {
            tool["name"] for tool in self.tool_registry.get_all_tools(visible_only=True)
        }
        self.system_prompt = self._compose_system_prompt(visible_tool_names)
        self._system_prompt_revision = self.tool_registry.revision
        self._workspace_instruction_revision = workspace_instruction_fingerprint()

    def _compose_system_prompt(self, visible_tool_names: set[str]) -> str:
        prompts = [
            build_skill_prompt(self.skills, visible_tool_names),
            build_identity_prompt(self.settings),
            build_workspace_instruction_prompt(),
        ]
        return "\n\n".join(prompt for prompt in prompts if prompt)

    def current_system_prompt(self) -> str:
        revision = self.tool_registry.revision
        workspace_revision = workspace_instruction_fingerprint()
        if revision != self._system_prompt_revision or workspace_revision != self._workspace_instruction_revision:
            visible_tool_names = {
                tool["name"] for tool in self.tool_registry.get_all_tools(visible_only=True)
            }
            self.system_prompt = self._compose_system_prompt(visible_tool_names)
            self._system_prompt_revision = revision
            self._workspace_instruction_revision = workspace_revision
        return self.system_prompt

    async def run(
        self,
        message: str,
        conversation_id: str,
        attachments: list[dict] | None = None,
    ) -> AsyncIterator[dict]:
        global _active_run_count
        self._active_runs += 1
        with _active_run_lock:
            _active_run_count += 1
        try:
            if _is_compact_command(message):
                result = await self.memory.compact_conversation(conversation_id)
                reply = _format_compact_reply(result)
                await self.memory.persist_turn(
                    conversation_id,
                    str(message or "").strip() or "/compact",
                    reply,
                    tool_calls=[],
                )
                if result.get("status") in {"compacted", "unchanged"} and str(result.get("summary") or "").strip():
                    source_message_id = self.memory._db.get_last_message_id(conversation_id)
                    if source_message_id > int(result.get("source_message_id") or 0):
                        self.memory._db.upsert_conversation_compaction(
                            conv_id=conversation_id,
                            summary=str(result.get("summary") or ""),
                            source_message_id=source_message_id,
                            message_count=int(result.get("message_count") or 0),
                            tokens_before=int(result.get("tokens_before") or 0),
                            tokens_after=int(result.get("tokens_after") or 0),
                        )
                yield {"event": "token", "data": {"content": reply}}
                yield {
                    "event": "done",
                    "data": {
                        "conversation_id": conversation_id,
                        "summary": reply,
                        "status": "complete",
                        "attachments": [],
                    },
                }
                return
            if _is_skill_creator_command(message):
                reply = _format_skill_creator_reply()
                await self.memory.persist_turn(
                    conversation_id,
                    str(message or "").strip() or "/skill creator",
                    reply,
                    tool_calls=[],
                )
                yield {"event": "token", "data": {"content": reply}}
                yield {
                    "event": "done",
                    "data": {
                        "conversation_id": conversation_id,
                        "summary": reply,
                        "status": "complete",
                        "attachments": [],
                    },
                }
                return
            async for event in self.turn_loop.run(
                message,
                conversation_id,
                system_prompt=self.current_system_prompt,
                attachments=attachments,
            ):
                yield event
        finally:
            self._active_runs = max(0, self._active_runs - 1)
            with _active_run_lock:
                _active_run_count = max(0, _active_run_count - 1)
            if self._retired and self._active_runs == 0:
                self.shutdown()

    def shutdown(self) -> None:
        self.turn_loop.shutdown()

    def retire(self) -> None:
        """Remove this runtime from the cache without killing active streams."""
        self._retired = True
        if self._active_runs == 0:
            self.shutdown()


_runtimes: dict[str, AgentRuntime] = {}
_active_run_lock = threading.Lock()
_active_run_count = 0


def _key_fingerprint(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _settings_cache_key(settings) -> str:
    provider = settings.model_provider
    provider_key = _provider_api_key(settings)
    skill_map = getattr(getattr(settings, "tools", object()), "skills", {}) or {}
    mcp_servers = getattr(getattr(settings, "mcp", object()), "servers", []) or []
    mcp_enabled = bool(getattr(getattr(settings, "mcp", object()), "enabled", True))
    browser_settings = getattr(settings, "browser", None)
    identity_settings = getattr(settings, "identity", None)
    llm_settings = getattr(settings, "llm", None)
    memory_settings = getattr(settings, "memory", None)
    try:
        mcp_fingerprint = json.dumps(mcp_servers, sort_keys=True)
    except TypeError:
        mcp_fingerprint = str(mcp_servers)
    try:
        browser_fingerprint = json.dumps(
            browser_settings if isinstance(browser_settings, dict) else vars(browser_settings),
            sort_keys=True,
        ) if browser_settings is not None else ""
    except TypeError:
        browser_fingerprint = str(browser_settings)
    try:
        identity_fingerprint = json.dumps(
            identity_settings if isinstance(identity_settings, dict) else vars(identity_settings),
            sort_keys=True,
        ) if identity_settings is not None else ""
    except TypeError:
        identity_fingerprint = str(identity_settings)
    try:
        memory_fingerprint = json.dumps(
            memory_settings if isinstance(memory_settings, dict) else vars(memory_settings),
            sort_keys=True,
        ) if memory_settings is not None else ""
    except TypeError:
        memory_fingerprint = str(memory_settings)
    parts = [
        provider,
        settings.model_name,
        settings.reasoning_effort,
        getattr(settings, "deepseek_base_url", ""),
        str(sorted(skill_map.items())),
        str(mcp_enabled),
        mcp_fingerprint,
        browser_fingerprint,
        identity_fingerprint,
        memory_fingerprint,
        str(getattr(llm_settings, "max_iterations_per_turn", "")),
        str(getattr(llm_settings, "max_turn_seconds", "")),
        str(getattr(llm_settings, "max_llm_call_seconds", "")),
        str(getattr(llm_settings, "vision_fallback_enabled", "")),
        str(getattr(llm_settings, "vision_fallback_model", "")),
        _key_fingerprint(getattr(settings, "google_api_key", "") or ""),
        _key_fingerprint(provider_key or ""),
    ]
    return "|".join(parts)


def _provider_api_key(settings) -> str:
    provider = str(getattr(settings, "model_provider", "openai") or "openai").lower()
    if provider == "gemini":
        return getattr(settings, "google_api_key", "")
    if provider == "deepseek":
        return getattr(settings, "deepseek_api_key", "")
    return getattr(settings, "openai_api_key", "")


def reset_runtime_cache(*, reset_mcp: bool = True, reset_browser: bool = False) -> None:
    try:
        from app.skills.mcp_bridge.connection import reset_mcp_runtime
    except Exception:
        reset_mcp_runtime = None
    try:
        from app.skills.browser_use.manager import reset_browser_use_runtime
    except Exception:
        reset_browser_use_runtime = None

    if reset_mcp and reset_mcp_runtime is not None:
        try:
            reset_mcp_runtime()
        except Exception:
            pass
    if reset_browser and reset_browser_use_runtime is not None:
        try:
            import asyncio

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(reset_browser_use_runtime())
            except RuntimeError:
                asyncio.run(reset_browser_use_runtime())
        except Exception:
            pass

    for runtime in _runtimes.values():
        runtime.retire()
    _runtimes.clear()


def active_runtime_run_count() -> int:
    with _active_run_lock:
        return _active_run_count


def get_runtime(settings) -> AgentRuntime:
    key = _settings_cache_key(settings)
    if key not in _runtimes:
        _runtimes[key] = AgentRuntime(settings)
    return _runtimes[key]


async def run_agent_stream(
    message: str,
    conversation_id: str,
    settings,
    attachments: list[dict] | None = None,
):
    runtime = get_runtime(settings)
    async for event in runtime.run(message, conversation_id, attachments=attachments):
        yield event
