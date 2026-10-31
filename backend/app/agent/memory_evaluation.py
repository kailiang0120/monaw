"""Repeatable evaluation shared by native and optional retrieval backends."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from app.agent.memory_prompt import MemoryPromptPolicy


class _PromptView(MemoryPromptPolicy):
    def __init__(self, canonical, hits):
        self.canonical, self.hits = canonical, hits
    def __getattr__(self, name):
        return getattr(self.canonical, name)
    def search(self, *args, **kwargs):
        return self.hits


async def _search(backend, query, workspace):
    if callable(getattr(backend, 'search_async', None)):
        return await backend.search_async(query, workspace=workspace)
    return await asyncio.to_thread(backend.search, query, workspace=workspace)


async def evaluate_retrieval_async(canonical: Any, backend: Any, workspace: str, other_workspace: str) -> dict:
    """Run only on an isolated empty store; this creates and edits fixture memories."""
    invoice = canonical.add('Deploy invoices with Helm charts', scope='workspace', workspace=workspace)
    tea = canonical.add('The user prefers jasmine tea', scope='global')
    private = canonical.add('Project nebula uses the cobalt queue', scope='workspace', workspace=other_workspace)
    cases = [('exact', 'Helm invoices', {invoice.id}), ('global', 'jasmine tea', {tea.id}),
             ('paraphrase', 'release billing through Kubernetes', {invoice.id}),
             ('no_match', 'quantum bicycles', set()), ('isolation', 'nebula cobalt queue', set())]
    hits = predicted = relevant = 0
    details, durations = [], []
    try:
        started = time.perf_counter()
        if callable(getattr(backend, 'prepare', None)):
            await asyncio.to_thread(backend.prepare, workspace)
        elif callable(getattr(backend, 'rebuild_index', None)):
            await asyncio.to_thread(backend.rebuild_index, workspace)
        ingest_ms = (time.perf_counter() - started) * 1000
        for name, query, expected in cases:
            started = time.perf_counter()
            results = await _search(backend, query, workspace)
            durations.append((time.perf_counter() - started) * 1000)
            actual = {item['id'] for item in results}
            hits += len(actual & expected)
            predicted += len(actual)
            relevant += len(expected)
            details.append({'case': name, 'expected': sorted(expected), 'actual': sorted(actual), 'passed': actual == expected})
        canonical.update(invoice.id, workspace=workspace, content='Deploy invoices with Kustomize')
        corrected_hits = await _search(backend, 'Kustomize invoices', workspace)
        canonical.delete(tea.id, workspace=workspace)
        forgotten = await _search(backend, 'jasmine tea', workspace)
        prompt = _PromptView(canonical, corrected_hits).build_prompt('Kustomize invoices', workspace=workspace)
        path = canonical.workspace_path(workspace)
        path.write_text(path.read_text(encoding='utf-8').replace('Kustomize', 'ArgoCD'), encoding='utf-8')
        manual_hits = await _search(backend, 'ArgoCD invoices', workspace)
        reopened = type(canonical)(root=canonical.root, settings=canonical.settings)
        if backend is canonical:
            restarted_hits = await _search(reopened, 'ArgoCD invoices', workspace)
        elif hasattr(backend, 'canonical'):
            previous = backend.canonical
            try:
                backend.canonical = reopened
                restarted_hits = await _search(backend, 'ArgoCD invoices', workspace)
            finally:
                backend.canonical = previous
        else:
            restarted_hits = []
        return {'recall': hits / relevant if relevant else 1, 'precision': hits / predicted if predicted else 1,
                'ingest_ms': ingest_ms, 'latency_ms_total': sum(durations), 'query_latency_ms': durations,
                'prompt_chars': len(prompt), 'cases': details,
                'correction_passed': any(item['id'] == invoice.id for item in corrected_hits),
                'forget_passed': all(item['id'] != tea.id for item in forgotten),
                'manual_edit_passed': any(item['id'] == invoice.id and 'ArgoCD' in item['content'] for item in manual_hits),
                'restart_passed': any(item['id'] == invoice.id and 'ArgoCD' in item['content'] for item in restarted_hits),
                'isolation_passed': all(private.id not in case['actual'] for case in details)}
    finally:
        if callable(getattr(backend, 'close', None)):
            await asyncio.to_thread(backend.close)


def evaluate_retrieval(canonical: Any, backend: Any, workspace: str, other_workspace: str) -> dict:
    return asyncio.run(evaluate_retrieval_async(canonical, backend, workspace, other_workspace))
