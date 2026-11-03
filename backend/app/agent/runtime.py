"""Agent runtime and turn orchestration."""

from __future__ import annotations

import hashlib
import json
import logging
import asyncio
import threading
from typing import AsyncIterator

from app.agent.llm_client import LLMClient
from app.agent.llm_constants import DEFAULT_MAX_TOKENS_PER_TURN
from app.agent.identity import DEFAULT_AGENT_NAME, LEGACY_AGENT_NAME
from app.agent.memory_store import get_memory_store
from app.agent.memory_manager import MemoryManager
from app.agent.skill_loader import load_tools
from app.agent.skill_prompt import build_skill_prompt
from app.agent.turn_loop import TurnLoop, conversation_lock
from app.agent.turn_text import conversation_title_from_prompt
from app.agent.steering import SteeringInbox
from app.agent.database import get_db
from app.agent.durable.turn_run import CONTINUATION_PROMPT, create_durable_run, resume_run
from app.agent.conversation_workspace import set_workspace_dir, reset_workspace_dir
from app.agent.workspace_instructions import (
    build_workspace_instruction_prompt,
    workspace_instruction_fingerprint,
)
from app.agent.run_context import (
    reset_current_control_session_id,
    reset_current_execution_source,
    reset_current_interactive,
    reset_current_permission_profile_id,
    reset_current_principal_id,
    set_current_control_session_id,
    set_current_execution_source,
    set_current_interactive,
    set_current_permission_profile_id,
    set_current_principal_id,
)

logger = logging.getLogger(__name__)


def _is_compact_command(message: str) -> bool:
    return str(message or "").strip().casefold() == "/compact"


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
            reasoning_effort=getattr(settings, "reasoning_effort", "medium"),
        )
        self.skills, self.tool_registry = load_tools(settings)
        self.memory = MemoryManager(self.llm_client)
        self.memory_store = get_memory_store(self.llm_client, settings)
        self.memory.set_memory_store(self.memory_store)

        llm_cfg = getattr(settings, "llm", None)
        # 0 is no limit for chats; unattended runs still stop at 200.
        max_iterations = getattr(llm_cfg, "max_iterations_per_turn", None) or 0
        max_turn_seconds = float(getattr(llm_cfg, "max_turn_seconds", None) or 7200)
        max_llm_call_seconds = float(getattr(llm_cfg, "max_llm_call_seconds", None) or 600)
        max_tokens_per_turn = getattr(llm_cfg, "max_tokens_per_turn", None)
        if max_tokens_per_turn is None:
            max_tokens_per_turn = DEFAULT_MAX_TOKENS_PER_TURN

        self.turn_loop = TurnLoop(
            llm_client=self.llm_client,
            registry=self.tool_registry,
            memory=self.memory,
            max_iterations=max_iterations,
            max_turn_seconds=max_turn_seconds,
            max_llm_call_seconds=max_llm_call_seconds,
            max_tokens_per_turn=max_tokens_per_turn,
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
        steering: SteeringInbox | None = None,
        durable_run=None,
        durable_run_factory=None,
    ) -> AsyncIterator[dict]:
        global _active_run_count
        conversation = get_db().get_conversation(conversation_id) or {}
        if durable_run is not None:
            saved_scope = durable_run.journal.load_run_internal(durable_run.run_id)
            if (
                saved_scope is None
                or str(saved_scope.get("workspace_dir") or "")
                != str(conversation.get("workspace_dir") or "")
            ):
                raise RuntimeError("Conversation workspace changed after the durable run was claimed.")
        workspace_token = set_workspace_dir(conversation.get("workspace_dir", ""))
        self._active_runs += 1
        with _active_run_lock:
            _active_run_count += 1
        try:
            if _is_compact_command(message):
                compact_lock = conversation_lock(conversation_id)
                await compact_lock.acquire()
                lease_stop = asyncio.Event()
                lease_task = None
                try:
                    if steering is not None:
                        steering.closed = True
                    if durable_run is None and callable(durable_run_factory):
                        durable_run = durable_run_factory()
                    if durable_run is not None:
                        async def _renew_compact_lease() -> None:
                            while not lease_stop.is_set():
                                try:
                                    await asyncio.wait_for(lease_stop.wait(), timeout=10.0)
                                    return
                                except asyncio.TimeoutError:
                                    durable_run.renew_lease()
                        lease_task = asyncio.create_task(_renew_compact_lease())
                    try:
                        result = await self.memory.compact_conversation(conversation_id)
                        reply = _format_compact_reply(result)
                        await self.memory.persist_turn(
                            conversation_id,
                            str(message or "").strip() or "/compact",
                            reply,
                            tool_calls=[],
                            **({"turn_id": durable_run.turn_id} if durable_run is not None else {}),
                        )
                        if result.get("status") in {"compacted", "unchanged"} and str(result.get("summary") or "").strip():
                            record_compaction = getattr(self.memory, "record_compaction", None)
                            if callable(record_compaction):
                                record_compaction(conversation_id, result)
                        if durable_run is not None:
                            if lease_task is not None and lease_task.done() and lease_task.exception() is not None:
                                raise lease_task.exception()
                            if not durable_run.finalize("complete"):
                                raise RuntimeError("durable compact run was not committed")
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
                    except Exception as exc:
                        logger.exception("Durable compact turn failed conversation_id=%s", conversation_id)
                        if durable_run is not None:
                            try:
                                durable_run.finalize("error", "persistence_failed")
                            except Exception:
                                logger.exception("Unable to finalize failed compact run run_id=%s", durable_run.run_id)
                        yield {"event": "error", "data": {
                            "code": "durable_run_failed",
                            "message": str(exc),
                        }}
                        yield {"event": "done", "data": {
                            "conversation_id": conversation_id,
                            "summary": "The compact operation could not be safely committed.",
                            "status": "error",
                            "incomplete": True,
                            "reason_code": "durable_run_failed",
                        }}
                finally:
                    lease_stop.set()
                    if lease_task is not None:
                        lease_task.cancel()
                        await asyncio.gather(lease_task, return_exceptions=True)
                    compact_lock.release()
                return
            async for event in self.turn_loop.run(
                message,
                conversation_id,
                system_prompt=self.current_system_prompt,
                attachments=attachments,
                **({"steering": steering} if steering is not None else {}),
                **({"durable_run": durable_run} if durable_run is not None else {}),
                **({"durable_run_factory": durable_run_factory} if durable_run is None and callable(durable_run_factory) else {}),
            ):
                yield event
        finally:
            reset_workspace_dir(workspace_token)
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


MAX_RUNTIME_CACHE = 3
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
        str(sorted(skill_map.items())),
        str(mcp_enabled),
        mcp_fingerprint,
        browser_fingerprint,
        identity_fingerprint,
        memory_fingerprint,
        str(getattr(llm_settings, "max_iterations_per_turn", "")),
        str(getattr(llm_settings, "max_turn_seconds", "")),
        str(getattr(llm_settings, "max_llm_call_seconds", "")),
        str(getattr(llm_settings, "max_tokens_per_turn", "")),
        _key_fingerprint(getattr(settings, "google_api_key", "") or ""),
        _key_fingerprint(provider_key or ""),
    ]
    return "|".join(parts)


def _provider_api_key(settings) -> str:
    provider = str(getattr(settings, "model_provider", "openai") or "openai").lower()
    if provider == "codex":
        return ""
    if provider == "gemini":
        return getattr(settings, "google_api_key", "")
    return getattr(settings, "openai_api_key", "")


def reset_runtime_cache(*, reset_mcp: bool = True) -> None:
    try:
        from app.skills.mcp_bridge.connection import reset_mcp_runtime
    except Exception:
        reset_mcp_runtime = None

    if reset_mcp and reset_mcp_runtime is not None:
        try:
            reset_mcp_runtime()
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
    runtime = _runtimes.pop(key, None)
    if runtime is not None:
        _runtimes[key] = runtime
        return runtime
    if len(_runtimes) >= MAX_RUNTIME_CACHE:
        oldest_key = next(iter(_runtimes))
        oldest = _runtimes.pop(oldest_key)
        oldest.retire()
    runtime = AgentRuntime(settings)
    _runtimes[key] = runtime
    return runtime


async def run_agent_stream(
    message: str,
    conversation_id: str,
    settings,
    attachments: list[dict] | None = None,
    control_session_id: str = "",
    execution_source: str = "desktop",
    principal_id: str = "",
    permission_profile_id: str = "",
    interactive: bool = True,
    steering: SteeringInbox | None = None,
    durable_run_id: str | None = None,
    durable_run=None,
):
    session_token = set_current_control_session_id(control_session_id)
    source_token = set_current_execution_source(execution_source)
    principal_token = set_current_principal_id(principal_id or control_session_id)
    profile_token = set_current_permission_profile_id(permission_profile_id)
    interactive_token = set_current_interactive(interactive)
    try:
        runtime = get_runtime(settings)
        try:
            database = get_db()
            conversation = database.get_conversation(conversation_id)
            if conversation is None:
                database.create_conversation(
                    conversation_id,
                    conversation_title_from_prompt(message),
                    "",
                )
                conversation = database.get_conversation(conversation_id) or {}
            stable_id = durable_run_id or (getattr(steering, "run_id", "") if steering is not None else "")
            workspace_dir = str(conversation.get("workspace_dir") or "")

            def durable_run_factory():
                latest_conversation = database.get_conversation(conversation_id) or {}
                if str(latest_conversation.get("workspace_dir") or "") != workspace_dir:
                    raise RuntimeError("Conversation workspace changed before the turn acquired ownership.")
                return create_durable_run(
                    run_id=stable_id or None,
                    conversation_id=conversation_id,
                    execution_source=execution_source,
                    principal_id=principal_id or control_session_id,
                    control_session_id=control_session_id,
                    workspace_dir=workspace_dir,
                )
            async for event in runtime.run(
                message, conversation_id, attachments=attachments,
                durable_run=durable_run,
                **({"durable_run_factory": durable_run_factory} if durable_run is None else {}),
                **({"steering": steering} if steering is not None else {}),
            ):
                yield event
        except Exception:
            logger.exception("Unable to start durable turn conversation_id=%s", conversation_id)
            if durable_run is not None:
                try:
                    durable_run.finalize("error", "persistence_failed")
                except Exception:
                    logger.exception("Unable to finalize failed durable turn run_id=%s", durable_run.run_id)
            yield {"event": "error", "data": {
                "code": "durable_run_unavailable",
                "message": "The run could not be safely started or finalized.",
            }}
            yield {"event": "done", "data": {
                "conversation_id": conversation_id,
                "summary": "The run could not be safely started or finalized.",
                "status": "error",
                "incomplete": True,
                "reason_code": "durable_run_unavailable",
            }}
    finally:
        reset_current_interactive(interactive_token)
        reset_current_permission_profile_id(profile_token)
        reset_current_principal_id(principal_token)
        reset_current_execution_source(source_token)
        reset_current_control_session_id(session_token)


async def resume_agent_stream(
    run_id: str,
    settings,
    *,
    execution_source: str,
    control_session_id: str,
    principal_id: str,
    permission_profile_id: str = "",
    interactive: bool = False,
):
    """Claim a validated paused run and continue from its committed history."""
    if execution_source not in {"desktop", "desktop_job", "desktop_stream"}:
        yield {"event": "error", "data": {
            "code": "resume_unavailable",
            "message": "This execution source cannot resume a saved run.",
        }}
        yield {"event": "done", "data": {
            "conversation_id": "",
            "summary": "This execution source cannot resume a saved run.",
            "status": "error",
            "incomplete": True,
            "reason_code": "resume_unavailable",
        }}
        return
    conversation = get_db().fetchone(
        """SELECT r.conversation_id, c.workspace_dir
           FROM agent_runs AS r JOIN conversations AS c ON c.id = r.conversation_id
           WHERE r.run_id = ?""",
        (run_id,),
    )
    if conversation is None:
        yield {"event": "error", "data": {"code": "run_not_found", "message": "The run is not available to resume."}}
        yield {"event": "done", "data": {
            "conversation_id": "",
            "summary": "The run is not available to resume.",
            "status": "error",
            "incomplete": True,
            "reason_code": "run_not_found",
        }}
        return
    conversation_id = str(conversation["conversation_id"])
    try:
        durable_run, _checkpoint = resume_run(
            run_id,
            execution_source=execution_source,
            principal_id=principal_id,
            control_session_id=control_session_id,
            workspace_dir=str(conversation["workspace_dir"] or ""),
        )
    except Exception:
        logger.exception("Unable to claim durable run for resume run_id=%s", run_id)
        yield {"event": "error", "data": {
            "code": "resume_unavailable",
            "message": "The saved run cannot be resumed safely from its current checkpoint.",
        }}
        yield {"event": "done", "data": {
            "conversation_id": conversation_id,
            "summary": "The saved run cannot be resumed safely.",
            "status": "error",
            "incomplete": True,
            "reason_code": "resume_unavailable",
        }}
        return

    async for event in run_agent_stream(
        message=CONTINUATION_PROMPT,
        conversation_id=conversation_id,
        settings=settings,
        attachments=None,
        control_session_id=control_session_id,
        execution_source=execution_source,
        principal_id=principal_id,
        permission_profile_id=permission_profile_id,
        interactive=interactive,
        durable_run=durable_run,
    ):
        yield event
