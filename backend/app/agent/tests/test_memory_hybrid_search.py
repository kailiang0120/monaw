from __future__ import annotations

from app.agent.database import Database
from app.agent import long_term_memory as memory_mod
from app.agent.long_term_memory import LongTermMemory


def _store(tmp_path) -> LongTermMemory:
    db = Database(tmp_path / "agent.db")
    db.init_db()
    return LongTermMemory(db=db, memory_root=tmp_path / "memory")


def test_lexical_search_prioritizes_token_matches(tmp_path):
    store = _store(tmp_path)
    target = store.remember(
        "The user likes automobiles for weekend travel.",
        category="preference",
        importance=8,
    )
    store.remember("The user works with spreadsheet exports.", category="workflow")

    results = store.score("automobiles travel", limit=2)

    assert target is not None
    assert results[0]["id"] == target["id"]
    assert results[0]["score_breakdown"]["token"] > 0


def test_lexical_search_order_is_deterministic(tmp_path):
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


def test_lexical_search_reuses_tokenized_memory_documents(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.remember("The user prefers concise implementation summaries.", category="preference")
    store.remember("The user works with spreadsheet exports.", category="workflow")
    store.remember("The user likes automobiles for weekend travel.", category="fact")

    token_calls = 0
    original_tokens = memory_mod._tokens

    def counted_tokens(text: str):
        nonlocal token_calls
        token_calls += 1
        return original_tokens(text)

    monkeypatch.setattr(memory_mod, "_tokens", counted_tokens)

    store.score("concise summaries", limit=2)
    first_call_count = token_calls
    store.score("weekend travel", limit=2)
    second_call_count = token_calls - first_call_count

    assert first_call_count > second_call_count
    assert second_call_count == 1

    store.remember("The user prefers direct progress updates.", category="preference")
    before_invalidated_search = token_calls
    store.score("direct updates", limit=2)

    assert token_calls - before_invalidated_search > second_call_count
