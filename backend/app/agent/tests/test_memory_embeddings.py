"""Semantic memory search through the user's saved OpenAI or Google key."""
import asyncio
import sys
from types import SimpleNamespace

import pytest

from app.agent.memory_index import (
    DEFAULT_EMBEDDING_MODELS,
    GEMINI_EMBEDDING_DIMENSIONS,
    EmbeddingProvider,
)
from app.agent.memory_store import MemoryStore
from app.agent.settings_store import MemorySettings
from app.config import settings as base_settings


def _store(tmp_path, monkeypatch, *, choice="auto", chat_provider="openai", openai_key="", google_key="", model=""):
    from app.agent import memory_retrieval
    monkeypatch.setattr(memory_retrieval, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(base_settings, "openai_api_key", openai_key)
    monkeypatch.setattr(base_settings, "google_api_key", google_key)
    settings = SimpleNamespace(
        memory=SimpleNamespace(enabled=True, semantic_search=choice, embedding_model=model, embedding_timeout=2),
        llm=SimpleNamespace(provider=chat_provider),
    )
    return MemoryStore(root=tmp_path / "memory", settings=settings)


@pytest.mark.parametrize(
    ("choice", "chat_provider", "openai_key", "google_key", "expected"),
    [
        ("auto", "gemini", "sk", "g", "gemini"),
        ("auto", "openai", "sk", "g", "openai"),
        ("auto", "codex", "", "g", "gemini"),
        ("auto", "codex", "", "", ""),
        ("off", "openai", "sk", "g", ""),
        ("openai", "gemini", "", "g", ""),
        ("local", "openai", "", "", "local"),
    ],
)
def test_semantic_provider_follows_the_saved_keys(tmp_path, monkeypatch, choice, chat_provider, openai_key, google_key, expected):
    store = _store(tmp_path, monkeypatch, choice=choice, chat_provider=chat_provider, openai_key=openai_key, google_key=google_key)

    assert store._semantic_provider() == expected


def test_auto_uses_each_providers_default_model(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, google_key="g", model="text-embedding-3-large")

    provider = store._index().provider

    assert (provider.mode, provider.model) == ("gemini", DEFAULT_EMBEDDING_MODELS["gemini"])
    assert store.index_health()["model"] == "gemini-embedding-001"


def test_without_keys_memory_uses_keywords_and_never_embeds(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    monkeypatch.setattr(EmbeddingProvider, "embed", lambda *a: (_ for _ in ()).throw(AssertionError("embedded")))
    store.add("The automobile is red.", scope="global")

    assert asyncio.run(store.search_async("automobile"))[0]["match_type"] == "keyword"
    health = store.index_health()
    assert health["status"] == "disabled"
    assert "OpenAI or Google API key" in health["error"]
    assert store.rebuild_index()["status"] == "disabled"


def test_a_chosen_provider_without_its_key_says_which_key(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, choice="openai", google_key="g")

    assert store.index_health() == {
        "mode": "openai", "status": "unconfigured", "entries": 0, "error": "Add an OpenAI API key in Connections.",
    }


def test_semantic_search_finds_a_paraphrase_with_the_saved_key(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, openai_key="sk")
    calls = []

    def embed(self, texts):
        calls.append((self.mode, self.model))
        return [[1.0, 0.0] if "automobile" in text or text == "car" else [0.0, 1.0] for text in texts]

    monkeypatch.setattr(EmbeddingProvider, "embed", embed)
    store.add("The automobile is red.", scope="global")

    results = asyncio.run(store.search_async("car"))

    assert [item["content"] for item in results] == ["The automobile is red."]
    assert results[0]["match_type"] == "semantic"
    assert calls[0] == ("openai", "text-embedding-3-small")


def test_gemini_embeds_memories_and_queries_as_retrieval_tasks(monkeypatch):
    monkeypatch.setattr(base_settings, "google_api_key", "g-test")
    requests = []

    class FakeModels:
        def embed_content(self, *, model, contents, config):
            requests.append((model, list(contents), config.task_type, config.output_dimensionality))
            return SimpleNamespace(embeddings=[SimpleNamespace(values=[0.6, 0.8]) for _ in contents])

    class FakeClient:
        def __init__(self, *, api_key, http_options):
            assert api_key == "g-test"
            self.models = FakeModels()

    from google import genai
    monkeypatch.setattr(genai, "Client", FakeClient)
    provider = EmbeddingProvider("gemini", "", timeout=3)

    assert provider.embed(["Likes tea", "Lives in Penang"]) == [[0.6, 0.8], [0.6, 0.8]]
    assert provider.embed_query("drinks") == [0.6, 0.8]
    assert requests == [
        ("gemini-embedding-001", ["Likes tea", "Lives in Penang"], "RETRIEVAL_DOCUMENT", GEMINI_EMBEDDING_DIMENSIONS),
        ("gemini-embedding-001", ["drinks"], "RETRIEVAL_QUERY", GEMINI_EMBEDDING_DIMENSIONS),
    ]


def test_openai_embeddings_use_the_saved_key_and_default_model(monkeypatch):
    monkeypatch.setattr(base_settings, "openai_api_key", "sk-test")
    calls = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            calls.append(kwargs["api_key"])
            self.embeddings = SimpleNamespace(create=lambda **kw: calls.append(kw) or SimpleNamespace(
                data=[SimpleNamespace(index=i, embedding=[1.0, 0.0]) for i, _ in enumerate(kw["input"])]))

        def close(self):
            calls.append("closed")

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))

    assert EmbeddingProvider("openai", "", timeout=3).embed_query("tea") == [1.0, 0.0]
    assert calls == ["sk-test", {"model": "text-embedding-3-small", "input": ["tea"]}, "closed"]


@pytest.mark.parametrize(
    ("saved", "expected"),
    [
        ({"embedding_mode": "keyword"}, "auto"),
        ({"embedding_mode": "provider", "embedding_model": "text-embedding-3-small"}, "openai"),
        ({"embedding_mode": "local"}, "local"),
        ({"embedding_mode": "keyword", "semantic_search": "off"}, "off"),
        ({}, "auto"),
    ],
)
def test_saved_embedding_mode_migrates_to_semantic_search(saved, expected):
    assert MemorySettings.model_validate(saved).semantic_search == expected
