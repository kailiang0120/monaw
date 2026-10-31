"""Rebuildable semantic cache; Markdown remains the only authoritative store."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from contextlib import closing, nullcontext
from urllib.parse import urlparse
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class EmbeddingError(RuntimeError):
    pass


DEFAULT_EMBEDDING_MODELS = {'openai': 'text-embedding-3-small', 'gemini': 'gemini-embedding-001'}
# Gemini returns 3072 values by default; 768 keeps the index small.
GEMINI_EMBEDDING_DIMENSIONS = 768
# Cosine similarity a memory needs to count as a match. Models spread their
# scores differently: Gemini rates unrelated text higher than OpenAI does.
# OpenAI's value is the best F1 on scripts/calibrate-memory-embeddings.py
# (related pairs median 0.42, unrelated median 0.09). Gemini's and the local
# value have not been measured yet; run that script with a Google key.
SIMILARITY_THRESHOLDS = {'openai': 0.3, 'gemini': 0.65, 'local': 0.45}


class EmbeddingProvider:
    def __init__(self, mode: str, model: str, endpoint: str = '', timeout: float = 5.0):
        self.mode, self.endpoint = mode, endpoint
        self.model = model or DEFAULT_EMBEDDING_MODELS.get(mode, '')
        self.timeout = max(1.0, min(float(timeout), 30.0))
        self.threshold = SIMILARITY_THRESHOLDS.get(mode, 0.45)

    @property
    def identity(self) -> str:
        return f'{self.mode}:{self.model}:{self.endpoint}'

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed saved memories."""
        return self._embed(texts, 'RETRIEVAL_DOCUMENT')

    def embed_query(self, text: str) -> list[float]:
        """Embed a search query. Gemini embeds queries differently from documents."""
        if self.mode == 'gemini':
            return self._embed([text], 'RETRIEVAL_QUERY')[0]
        return self.embed([text])[0]

    def _embed(self, texts: list[str], task: str) -> list[list[float]]:
        if not texts:
            return []
        if self.mode == 'local':
            parsed = urlparse(self.endpoint)
            if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or not self.model:
                raise EmbeddingError('Local embeddings require a loopback HTTP endpoint and model')
            request = Request(self.endpoint, data=json.dumps({'model': self.model, 'input': texts}).encode(), headers={'Content-Type': 'application/json'})
            with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=self.timeout) as response:
                payload = json.load(response)
            vectors = [item['embedding'] for item in sorted(payload['data'], key=lambda item: item['index'])]
        elif self.mode == 'openai':
            from app.config import settings
            if not settings.openai_api_key:
                raise EmbeddingError('An OpenAI API key is required')
            from openai import OpenAI
            client = OpenAI(api_key=settings.openai_api_key, timeout=self.timeout, max_retries=0)
            try:
                response = client.embeddings.create(model=self.model, input=texts)
                vectors = [item.embedding for item in sorted(response.data, key=lambda item: item.index)]
            finally:
                client.close()
        elif self.mode == 'gemini':
            from app.config import settings
            if not settings.google_api_key:
                raise EmbeddingError('A Google API key is required')
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=settings.google_api_key, http_options=types.HttpOptions(timeout=int(self.timeout * 1000)))
            response = client.models.embed_content(
                model=self.model,
                contents=texts,
                config=types.EmbedContentConfig(task_type=task, output_dimensionality=GEMINI_EMBEDDING_DIMENSIONS),
            )
            vectors = [list(item.values or []) for item in response.embeddings or []]
        else:
            raise EmbeddingError('Semantic embeddings are disabled')
        if len(vectors) != len(texts) or not vectors or not vectors[0]:
            raise EmbeddingError('Invalid embedding response')
        dimension = len(vectors[0])
        if any(len(v) != dimension or any(not isinstance(x, (float, int)) or not math.isfinite(x) for x in v) for v in vectors):
            raise EmbeddingError('Invalid embedding dimensions or values')
        return vectors


class SemanticIndex:
    def __init__(self, path: Path, provider: EmbeddingProvider):
        self.path, self.provider = path, provider

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=2)
        try:
            conn.execute('CREATE TABLE IF NOT EXISTS vectors (scope TEXT, workspace TEXT, id TEXT, hash TEXT, model TEXT, dimension INTEGER, updated TEXT, vector TEXT, PRIMARY KEY(scope, workspace, id))')
            return conn
        except BaseException:
            conn.close()
            raise

    def rebuild(self, entries, guard=None, replace_scopes=None):
        # Embed before replacing cache: failure cannot damage existing index.
        vectors = []
        deadline = time.monotonic() + self.provider.timeout
        for start in range(0, len(entries), 32):
            if time.monotonic() >= deadline:
                raise EmbeddingError('Index rebuild timed out')
            vectors.extend(self.provider.embed([e.content for e in entries[start:start + 32]]))
            if time.monotonic() >= deadline:
                raise EmbeddingError('Index rebuild timed out')
        with guard(entries) if guard else nullcontext():
            with closing(self._connect()) as conn, conn:
                for scope, workspace in replace_scopes or []:
                    conn.execute('DELETE FROM vectors WHERE scope=? AND workspace=?', (scope, workspace))
                for entry, vector in zip(entries, vectors):
                    conn.execute('INSERT OR REPLACE INTO vectors VALUES (?,?,?,?,?,?,?,?)',
                                 (entry.scope, entry.workspace, entry.id, content_hash(entry.content), self.provider.identity,
                                  len(vector), entry.updated_at, json.dumps(vector)))
        return len(vectors)

    def search(self, query: str, entries, guard=None, retried=False):
        if not entries:
            return []
        # Canonical entries are already scope-filtered. Only matching hashes and model are eligible.
        with closing(self._connect()) as conn, conn:
            cached = {}
            for e in entries:
                row = conn.execute('SELECT hash, model, dimension, vector FROM vectors WHERE scope=? AND workspace=? AND id=?', (e.scope, e.workspace, e.id)).fetchone()
                if row is not None:
                    cached[(e.scope, e.workspace, e.id)] = row
        stale = [e for e in entries if (r := cached.get((e.scope, e.workspace, e.id))) is None or r[0] != content_hash(e.content) or r[1] != self.provider.identity]
        if stale:
            if retried:
                raise EmbeddingError('Index changed during retrieval')
            self.rebuild(stale, guard)
            return self.search(query, entries, guard, True)
        query_vector = self.provider.embed_query(query)
        ranked = []
        for e in entries:
            h, model, dimension, raw = cached[(e.scope, e.workspace, e.id)]
            vector = json.loads(raw)
            if dimension != len(query_vector) or len(vector) != dimension:
                self.rebuild(entries, guard)
                raise EmbeddingError('Embedding dimension changed; index rebuilt; retry search')
            denom = math.sqrt(sum(x*x for x in vector) * sum(x*x for x in query_vector))
            similarity = sum(a*b for a, b in zip(vector, query_vector)) / denom if denom else 0.0
            if similarity >= self.provider.threshold:
                ranked.append((e, similarity))
        return sorted(ranked, key=lambda pair: (-pair[1], pair[0].id))

    def remove(self, entry):
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM vectors WHERE scope=? AND workspace=? AND id=?", (entry.scope, entry.workspace, entry.id))

    def clear(self):
        with closing(self._connect()) as conn, conn:
            conn.execute('DELETE FROM vectors')

    def health(self):
        try:
            with closing(self._connect()) as conn, conn:
                count = conn.execute('SELECT count(*) FROM vectors').fetchone()[0]
            return {'status': 'ready', 'entries': count, 'provider': self.provider.mode}
        except (OSError, sqlite3.Error) as exc:
            return {'status': 'error', 'entries': 0, 'provider': self.provider.mode, 'error': str(exc)}
