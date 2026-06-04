from __future__ import annotations

import asyncio
import json

from app.agent.database import Database
from app.agent.long_term_memory import LongTermMemory, _write_markdown
from app.skills.memory import tools as memory_tools


class CuratorLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, system_prompt: str = "", stream_callback=None):  # noqa: ARG002
        self.calls += 1
        return json.dumps(
            {
                "decisions": [
                    {
                        "action": "remember",
                        "content": "The user's current project context is a FastAPI automation app.",
                        "category": "project",
                        "confidence": 0.91,
                        "importance": 6,
                    },
                    {
                        "action": "session_summary",
                        "content": "The session covered memory curation for a FastAPI automation app.",
                        "category": "reflection",
                        "confidence": 0.85,
                        "importance": 5,
                    },
                ]
            }
        )


class MergeLLM:
    async def chat(self, messages, system_prompt: str = "", stream_callback=None):  # noqa: ARG002
        return json.dumps(
            {
                "action": "merge",
                "section_id": "sec_comm",
                "merged_body": "- Prefers concise engineering summaries.\n- Prefers direct progress updates.",
                "reasoning": "same preference topic",
            }
        )


def _store(tmp_path, *, llm_client=None) -> LongTermMemory:
    db = Database(tmp_path / "agent.db")
    db.init_db()
    return LongTermMemory(db=db, llm_client=llm_client, memory_root=tmp_path / "memory")


def test_memory_remember_search_and_dedupe_writes_markdown(tmp_path):
    store = _store(tmp_path)

    first = store.remember(
        "The user prefers concise engineering summaries.",
        category="preference",
        review_state="reviewed",
    )
    second = store.remember(
        "The user prefers concise engineering summaries.",
        category="preference",
        review_state="new",
    )

    assert first is not None
    assert second is not None
    assert first["id"] == second["id"]
    assert (tmp_path / "memory" / "long-term" / "preference.md").exists()

    results = store.search("concise summaries", limit=5)

    assert len(results) == 1
    assert results[0]["category"] == "preference"


def test_memory_migrates_legacy_style_into_sectioned_preference(tmp_path):
    root = tmp_path / "memory"
    legacy = root / "long-term" / "style"
    legacy.mkdir(parents=True)
    _write_markdown(
        legacy / "short.md",
        {
            "id": "legacy-style",
            "category": "style",
            "status": "active",
            "review_state": "reviewed",
            "confidence": 1,
            "importance": 8,
            "kind": "fact",
            "created_at": "2026-05-10T00:00:00+00:00",
            "updated_at": "2026-05-10T00:00:00+00:00",
        },
        "The user prefers short answers.",
    )

    store = _store(tmp_path)
    memories = store.list_memories(category="preference", status="active")

    assert (root / "long-term" / "preference.md").exists()
    assert (root / "archive" / "legacy-memories" / "long-term" / "style" / "short.md").exists()
    assert memories[0]["id"] == "legacy-style"
    assert memories[0]["category"] == "preference"


def test_memory_llm_merge_updates_existing_section(tmp_path):
    store = _store(tmp_path, llm_client=MergeLLM())
    store.save_memory_file(
        "preference",
        """---
category: preference
schema: sectioned-v1
updated_at: 2026-05-10T00:00:00+00:00
---

## Communication {#sec_comm}
<!-- meta: {"id":"sec_comm","importance":7,"confidence":1.0,"created_at":"2026-05-10T00:00:00+00:00","updated_at":"2026-05-10T00:00:00+00:00","review_state":"reviewed","kind":"fact","status":"active"} -->

- Prefers concise engineering summaries.
""",
    )

    saved = store.remember("The user prefers direct progress updates.", category="preference")
    memories = store.list_memories(category="preference", status="active")

    assert saved is not None
    assert saved["id"] == "sec_comm"
    assert len(memories) == 1
    assert "direct progress updates" in memories[0]["content"]


def test_memory_lexical_search_is_deterministic(tmp_path):
    store = _store(tmp_path)
    first = store.remember(
        "The user prefers concise implementation summaries.",
        category="preference",
        importance=8,
    )
    second = store.remember(
        "The user prefers verbose meeting notes.",
        category="preference",
        importance=4,
    )

    run_one = store.score("concise summary", limit=2)
    run_two = store.score("concise summary", limit=2)

    assert first is not None
    assert second is not None
    assert [item["id"] for item in run_one] == [item["id"] for item in run_two]
    assert run_one[0]["id"] == first["id"]
    assert set(run_one[0]["score_breakdown"]) == {"exact", "token", "category", "recency", "importance", "use"}


def test_memory_prompt_includes_personality_and_relevant_markdown(tmp_path):
    store = _store(tmp_path)
    store.settings = type(
        "Settings",
        (),
        {
            "memory": type(
                "MemorySettings",
                (),
                {
                    "enabled": True,
                    "retrieval_limit": 6,
                    "max_injected_chars": 600,
                    "min_confidence": 0.75,
                    "min_relevance_score": 0.15,
                },
            )()
        },
    )()
    store.remember("The user works on tax spreadsheet automation.", category="project")
    store.remember("The user prefers concise summaries.", category="preference", importance=7)
    store._update_personality("user", "The user is a hands-on builder.", "conv-personality")

    prompt = store.build_prompt("tax spreadsheet")

    assert "## What I know about you" in prompt
    assert "Personality" in prompt
    assert "You prefer concise summaries." in prompt
    assert "tax spreadsheet" in prompt
    assert any(memory["use_count"] > 0 for memory in store.list_memories(status="active"))
    audit = store.audit_log(limit=5)
    assert any(item["action"] == "INJECT" for item in audit)


def test_memory_threshold_settings_allow_explicit_zero(tmp_path):
    store = _store(tmp_path)
    store.settings = type(
        "Settings",
        (),
        {
            "memory": type(
                "MemorySettings",
                (),
                {
                    "min_confidence": 0,
                    "min_relevance_score": 0,
                },
            )()
        },
    )()

    assert store.min_confidence == 0.0
    assert store.min_relevance_score == 0.0


def test_session_close_curator_rejects_sensitive_and_saves_decisions(tmp_path):
    llm = CuratorLLM()
    store = _store(tmp_path, llm_client=llm)
    store._db.create_conversation("conv-1", "Memory")
    store._db.add_message("conv-1", "user", "I like direct progress updates in this FastAPI automation app.")
    store._db.add_message("conv-1", "assistant", "Understood.")

    rejected = store.remember("The user's API key is sk-secret.", category="fact")
    result = asyncio.run(store.curate_session("conv-1"))
    saved = store.list_memories(status="active")

    assert rejected is None
    assert llm.calls >= 1
    assert result["added"] >= 2
    assert (tmp_path / "memory" / "short-term" / "session-conv-1.md").exists()
    assert {memory["review_state"] for memory in saved} == {"new"}
    assert any(memory["category"] == "project" for memory in saved)


def test_auto_learning_is_noop_until_session_close(tmp_path):
    store = _store(tmp_path)
    staged = asyncio.run(
        store.learn_from_turn(
            conversation_id="conv-skip",
            user_message="I prefer Python.",
            assistant_message="Noted.",
        )
    )

    assert len(staged) == 1
    assert staged[0]["category"] == "preference"
    assert store.list_memories(status="active") == []
    assert len(store.list_candidates()) == 1


def test_turn_learning_auto_reviewed_writes_multiple_feature_memories(tmp_path):
    store = _store(tmp_path)
    store.settings = type(
        "Settings",
        (),
        {
            "memory": type(
                "MemorySettings",
                (),
                {
                    "enabled": True,
                    "auto_learn": True,
                    "curate_on_session_close": True,
                    "write_policy": "auto_reviewed",
                    "retrieval_limit": 6,
                    "max_injected_chars": 2500,
                    "min_confidence": 0.75,
                    "min_relevance_score": 0.15,
                    "maintenance_cooldown_hours": 24,
                },
            )()
        },
    )()

    staged = store.capture_turn_candidates(
        conversation_id="conv-turn-reviewed",
        user_message="My name is Kai. I work on the Monaw repo. Please always keep updates concise.",
        assistant_message="Noted.",
    )
    memories = store.list_memories(status="active")

    assert len(staged) >= 3
    assert store.list_candidates() == []
    assert any(memory["category"] == "project" and "monaw repo" in memory["content"].lower() for memory in memories)
    assert any(memory["category"] == "behavior" and "always keep updates concise" in memory["content"].lower() for memory in memories)
    prompt = store.build_prompt("repo updates")
    assert "Monaw repo" in prompt
    assert "preferred name is Kai" in prompt


def test_prompt_retrieval_skips_short_term_session_summaries(tmp_path):
    store = _store(tmp_path)
    store.remember("The user prefers concise summaries.", category="preference", importance=8)
    store._write_session_summary(
        "conv-short-term",
        "Session conv-short-term had 22 messages. Goal: generic recap.",
        source="test",
    )

    prompt = store.build_prompt("concise summaries")

    assert "You prefer concise summaries." in prompt
    assert "Session conv-short-term had 22 messages" not in prompt


def test_short_high_signal_turns_bypass_length_floor(tmp_path):
    store = _store(tmp_path)

    for text in (
        "I'm a developer",
        "I work in finance",
        "I'm Kai",
        "my name is Kai",
        "use Python",
        "use tabs not spaces",
    ):
        assert store._should_skip_learning(text) is False


def test_recall_memory_writes_explicit_telemetry(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.remember("The user prefers Python for automation.", category="preference")
    monkeypatch.setattr(memory_tools, "get_long_term_memory", lambda: store)

    payload = json.loads(memory_tools._recall_memory("Python automation", category="preference"))
    audit = store.audit_log(limit=5)
    recall_event = next(item for item in audit if item["action"] == "RECALL")
    telemetry = json.loads(recall_event["candidate_content"])

    assert payload["status"] == "ok"
    assert telemetry["recall_memory_called"] is True
    assert telemetry["query"] == "Python automation"
    assert telemetry["category"] == "preference"


def test_profile_candidates_and_checkpoints_are_persistent(tmp_path):
    store = _store(tmp_path)

    profile = store.update_profile_field(
        "github_username",
        "kai-liang",
        privacy_level="normal",
        review_state="reviewed",
    )
    captured = store.capture_turn_candidates(
        conversation_id="conv-profile",
        user_message="I prefer Python for automation.",
        assistant_message="Noted.",
    )
    checkpoint = store.upsert_checkpoint(
        "conversation-conv-profile",
        scope="conversation",
        status="active",
        conversation_id="conv-profile",
        goal="Build memory system",
        last_known_state="Implementation started.",
        next_action="Run tests.",
    )

    assert profile["field"] == "github_username"
    assert store.profile_fields()[0]["value"] == "kai-liang"
    assert len(captured) == 1
    assert store.list_candidates()[0]["category"] == "preference"
    assert checkpoint["status"] == "active"
    assert store.list_checkpoints()[0]["next_action"] == "Run tests."


def test_approving_candidate_promotes_to_reviewed_memory(tmp_path):
    store = _store(tmp_path)
    candidate = store.add_candidate(
        "The user prefers direct progress updates.",
        category="preference",
        confidence=0.9,
        importance=8,
        source_conversation_id="conv-candidate",
    )

    assert candidate is not None
    updated = store.update_candidate(candidate["id"], status="approved", approve=True)
    memories = store.list_memories(category="preference", status="active")

    assert updated is not None
    assert updated["status"] == "approved"
    assert len(memories) == 1
    assert memories[0]["review_state"] == "reviewed"
