"""Independent regression checks for the memory transformation review."""
import asyncio
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from app.agent.memory_index import EmbeddingProvider
from app.agent.memory_store import MemoryStore


@pytest.fixture
def hybrid(tmp_path, monkeypatch):
    from app.agent import memory_retrieval
    monkeypatch.setattr(memory_retrieval, "RUNTIME_DIR", tmp_path / "runtime")
    settings = SimpleNamespace(memory=SimpleNamespace(
        enabled=True, embedding_mode="local", embedding_model="review-model",
        embedding_endpoint="http://127.0.0.1:1/v1/embeddings", embedding_timeout=2,
    ))
    return MemoryStore(root=tmp_path / "memory", settings=settings)


def test_forget_during_embedding_cannot_restore_vector(hybrid, monkeypatch):
    started, release = threading.Event(), threading.Event()
    def embed(self, texts):
        started.set()
        assert release.wait(3)
        return [[1.0, 0.0] for _ in texts]
    monkeypatch.setattr(EmbeddingProvider, "embed", embed)
    entry = hybrid.add("The automobile is red.", scope="global")
    hybrid.search("car")
    jobs = list(hybrid._semantic_jobs.values())
    try:
        assert started.wait(2)
        assert hybrid.delete(entry.id)
    finally:
        release.set()
    for job in jobs:
        try:
            job.result(timeout=3)
        except ValueError:
            pass  # The canonical guard correctly rejected the deleted input.
    assert asyncio.run(hybrid.search_async("car")) == []
    path = hybrid._index().path
    if path.exists():
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT count(*) FROM vectors").fetchone()[0] == 0


def test_disabled_async_search_does_not_send_memory(hybrid, monkeypatch):
    calls = []
    monkeypatch.setattr(EmbeddingProvider, "embed", lambda self, texts: calls.append(texts) or [[1.0] for _ in texts])
    hybrid.add("The automobile is red.", scope="global")
    hybrid.settings.memory.enabled = False
    asyncio.run(hybrid.search_async("car"))
    assert calls == []


def test_manual_edit_invalidates_cached_semantic_result(hybrid, monkeypatch):
    def embed(self, texts):
        return [[1.0, 0.0] if "automobile" in text or text == "car" else [0.0, 1.0] for text in texts]
    monkeypatch.setattr(EmbeddingProvider, "embed", embed)
    hybrid.add("The automobile is red.", scope="global")
    assert len(asyncio.run(hybrid.search_async("car"))) == 1
    raw = hybrid.global_path.read_text(encoding="utf-8")
    hybrid.global_path.write_text(raw.replace("The automobile is red.", "The bicycle is green."), encoding="utf-8")
    assert asyncio.run(hybrid.search_async("car")) == []


def test_corrupt_index_rebuild_preserves_markdown(hybrid, monkeypatch):
    monkeypatch.setattr(EmbeddingProvider, "embed", lambda self, texts: [[1.0, 0.0] for _ in texts])
    entry = hybrid.add("The automobile is red.", scope="global")
    path = hybrid._index().path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a sqlite database")
    assert hybrid.rebuild_index()["status"] == "ready"
    assert hybrid.find(entry.id).content == entry.content
    assert asyncio.run(hybrid.search_async("car"))[0]["id"] == entry.id


def test_first_memory_access_honors_saved_settings_before_chat(monkeypatch):
    from app.agent import settings_store
    from app.agent.memory_store import get_memory_store, reset_memory_store
    saved = SimpleNamespace(memory=SimpleNamespace(enabled=False, embedding_mode="provider"))
    monkeypatch.setattr(settings_store, "load_agent_settings", lambda *args, **kwargs: saved)
    reset_memory_store()
    assert get_memory_store().enabled is False


def test_local_http_embeddings_feed_first_query_prompt(hybrid):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            payload = json.dumps({"data": [
                {"index": i, "embedding": [1.0, 0.0]}
                for i in range(len(body["input"]))
            ]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        hybrid.settings.memory.embedding_endpoint = f"http://127.0.0.1:{server.server_port}/v1/embeddings"
        hybrid.add("The automobile is red.", scope="global")
        prompt = asyncio.run(hybrid.build_prompt_async("car"))
        assert "The automobile is red." in prompt
        assert [item["input"] for item in requests] == [["The automobile is red."], ["car"]]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_embedding_wait_keeps_event_loop_responsive(hybrid, monkeypatch):
    started, release = threading.Event(), threading.Event()
    def embed(self, texts):
        started.set()
        assert release.wait(3)
        return [[1.0, 0.0] for _ in texts]
    monkeypatch.setattr(EmbeddingProvider, "embed", embed)
    hybrid.add("The automobile is red.", scope="global")

    async def scenario():
        task = asyncio.create_task(hybrid.build_prompt_async("car"))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            assert not task.done()
            await asyncio.sleep(0)  # Other callbacks can run while embedding is blocked.
        finally:
            release.set()
        assert "automobile" in await task

    asyncio.run(scenario())


def test_clear_prevents_inflight_learning_from_restoring_memory(hybrid):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        class SlowLearner:
            async def chat(self, *args, **kwargs):
                started.set()
                await release.wait()
                return json.dumps({"operations": [{"op": "add", "scope": "global", "content": "Likes jasmine tea."}]})
        hybrid.llm_client = SlowLearner()
        task = hybrid.schedule_learning(user_message="Remember that I like jasmine tea.")
        assert task is not None
        await started.wait()
        hybrid.clear()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert hybrid.entries() == []
    asyncio.run(scenario())
