"""Measure how an embedding provider scores related and unrelated memory pairs.

Reads OPENAI_API_KEY and GOOGLE_API_KEY from the environment and runs each
provider whose key is set. It prints similarity scores only, never a key.
The cutoff it suggests is where SIMILARITY_THRESHOLDS in
backend/app/agent/memory_index.py should sit for that provider.

    uv run --project backend python scripts/calibrate-memory-embeddings.py
"""
from __future__ import annotations

import math
import os
import statistics
import sys
from pathlib import Path

os.environ.setdefault("MONAW_CONTROL_SECRET", "calibration-control-secret-32-chars")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.agent.memory_index import EmbeddingProvider, SIMILARITY_THRESHOLDS  # noqa: E402
from app.config import settings  # noqa: E402

MEMORIES = {
    "brief": "Prefers short answers that lead with the conclusion.",
    "shell": "Uses Windows 11 and PowerShell 7 for development.",
    "framework": "The Monaw backend runs on FastAPI with Python 3.11.",
    "staging": "Deploys the staging site every Friday afternoon.",
    "allergy": "Allergic to peanuts.",
    "car": "Drives a red Toyota Corolla.",
    "job": "Works as an auditor at an accounting firm in Kuala Lumpur.",
    "invoices": "Wants invoices exported as PDF, not Word documents.",
    "database": "The project's database is SQLite, stored in the runtime folder.",
    "cat": "Has a cat named Mochi.",
    "branch": "Never commit directly to the main branch; open a pull request.",
    "theme": "Prefers dark mode in every app.",
    "japanese": "Learning Japanese, currently at JLPT N4 level.",
    "ci": "The CI pipeline runs on GitHub Actions.",
    "meeting": "Meets the finance team every Monday at 9 am.",
    "uv": "Uses uv to manage Python dependencies.",
    "food": "Favourite food is nasi lemak.",
    "attribution": "Keep commit messages free of AI attribution lines.",
}

# Each question names the memories that should match it. Most avoid the
# memory's own words, so keyword search would miss them.
QUERIES = {
    "Keep it brief.": {"brief"},
    "Which terminal do I work in?": {"shell"},
    "What web framework is the server built on?": {"framework"},
    "When do we push changes to the test environment?": {"staging"},
    "Can I eat satay with its sauce?": {"allergy"},
    "What vehicle do I own?": {"car"},
    "What do I do for a living?": {"job"},
    "Which format should billing documents be saved in?": {"invoices"},
    "Where is the app's data kept?": {"database"},
    "What is my pet called?": {"cat"},
    "How should my changes get into the main code line?": {"branch"},
    "Light or dark colour scheme?": {"theme"},
    "Which language am I studying?": {"japanese"},
    "Where do the automated checks run?": {"ci"},
    "What's on my calendar at the start of the week?": {"meeting"},
    "How do I add a Python package to this project?": {"uv"},
    "Suggest a dish I would enjoy for lunch.": {"food", "allergy"},
    "How should I write the commit message?": {"attribution", "branch"},
    "What's the weather like in Paris today?": set(),
    "Explain quantum entanglement simply.": set(),
    "Translate 'good morning' into French.": set(),
    "How tall is Mount Everest?": set(),
    "Write a haiku about autumn leaves.": set(),
}


def cosine(a: list[float], b: list[float]) -> float:
    denom = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b)) / denom if denom else 0.0


def evaluate(pairs: list[tuple[float, bool]], cutoff: float) -> tuple[float, float, float, int]:
    hits = sum(1 for score, related in pairs if related and score >= cutoff)
    false_hits = sum(1 for score, related in pairs if not related and score >= cutoff)
    related = sum(1 for _, rel in pairs if rel)
    precision = hits / (hits + false_hits) if hits + false_hits else 0.0
    recall = hits / related if related else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1, false_hits


def calibrate(mode: str) -> None:
    provider = EmbeddingProvider(mode, "", timeout=30)
    ids = list(MEMORIES)
    memory_vectors = dict(zip(ids, provider.embed([MEMORIES[i] for i in ids])))
    pairs: list[tuple[float, bool]] = []
    print(f"\n== {mode}: {provider.model}")
    for query, relevant in QUERIES.items():
        vector = provider.embed_query(query)
        scores = sorted(((cosine(vector, memory_vectors[i]), i) for i in ids), reverse=True)
        pairs.extend((score, i in relevant) for score, i in scores)
        top = ", ".join(f"{i} {score:.3f}" for score, i in scores[:3])
        expected = ", ".join(sorted(relevant)) or "none"
        print(f"  {query[:52]:<52} expected {expected:<18} top {top}")
    related = sorted(score for score, rel in pairs if rel)
    unrelated = sorted(score for score, rel in pairs if not rel)
    print(f"  related pairs   n={len(related):<4} min {related[0]:.3f}  median {statistics.median(related):.3f}")
    print(f"  unrelated pairs n={len(unrelated):<4} median {statistics.median(unrelated):.3f}"
          f"  95th pct {unrelated[int(len(unrelated) * 0.95)]:.3f}  max {unrelated[-1]:.3f}")
    candidates = [round(0.15 + step * 0.01, 2) for step in range(76)]
    best = max(candidates, key=lambda cutoff: (evaluate(pairs, cutoff)[2], cutoff))
    for label, cutoff in (("current", SIMILARITY_THRESHOLDS.get(mode, 0.45)), ("best F1", best)):
        precision, recall, f1, false_hits = evaluate(pairs, cutoff)
        print(f"  {label:<8} cutoff {cutoff:.2f}: precision {precision:.2f}  recall {recall:.2f}"
              f"  F1 {f1:.2f}  unrelated matches {false_hits}")


def main() -> int:
    settings.openai_api_key = os.environ.get("OPENAI_API_KEY", "")
    settings.google_api_key = os.environ.get("GOOGLE_API_KEY", "")
    modes = [mode for mode, key in (("openai", settings.openai_api_key), ("gemini", settings.google_api_key)) if key]
    if not modes:
        print("Set OPENAI_API_KEY or GOOGLE_API_KEY to calibrate.")
        return 1
    for mode in modes:
        calibrate(mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
