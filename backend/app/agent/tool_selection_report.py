"""Summarize shadow-mode tool-selection decisions recorded by the turn loop.

Each ``tool_selection_shadow`` event stores the decision model's full
probability distribution, so other selection thresholds can be replayed
offline without calling the model again::

    python -m app.agent.tool_selection_report
    python -m app.agent.tool_selection_report --db path/to/observability.sqlite3 --limit 500
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from app.agent.tool_selection import select_domains

DEFAULT_COVERAGES = (0.7, 0.8, 0.9, 0.95)
DEFAULT_MIN_CONFIDENCES = (0.0, 0.3, 0.4, 0.5, 0.6)


def load_shadow_events(db_path: Path, *, limit: int = 1000) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT metadata_json FROM observability_events "
            "WHERE event_type = 'tool_selection_shadow' ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    events = []
    for (raw,) in rows:
        try:
            events.append(json.loads(raw or "{}"))
        except ValueError:
            continue
    return events


def recent_shadow_stats(db_path: Path, *, since: str) -> dict[str, Any]:
    """Counts for decisions logged at or after ``since`` (an ISO timestamp)."""
    stats: dict[str, Any] = {
        "decisions": 0,
        "scored": 0,
        "covered": 0,
        "last_decision_at": None,
        "last_status": "",
        "last_top_domain": "",
    }
    if not db_path.exists():
        return stats
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT created_at, metadata_json FROM observability_events "
            "WHERE event_type = 'tool_selection_shadow' AND created_at >= ? ORDER BY created_at DESC",
            (since,),
        ).fetchall()
    finally:
        conn.close()
    for index, (created_at, raw) in enumerate(rows):
        try:
            event = json.loads(raw or "{}")
        except ValueError:
            event = {}
        stats["decisions"] += 1
        if event.get("covered") is not None:
            stats["scored"] += 1
            stats["covered"] += int(bool(event["covered"]))
        if index == 0:
            stats["last_decision_at"] = created_at
            stats["last_status"] = str(event.get("status") or "")
            stats["last_top_domain"] = str(event.get("top_domain") or "")
    return stats


def replay_policy(
    events: Iterable[dict[str, Any]],
    *,
    coverage: float,
    min_confidence: float,
    max_domains: int,
) -> dict[str, Any]:
    """Score one threshold setting against turns where the model answered."""
    decided = covered = fallbacks = 0
    kept_tools = visible_tools = 0
    for event in events:
        probabilities = event.get("probabilities") or {}
        if event.get("status") not in {"ok", "low_confidence"} or not probabilities:
            continue
        counts: dict[str, int] = event.get("domain_tool_counts") or {}
        used = set(event.get("used_domains") or [])
        decided += 1
        visible_tools += sum(counts.values())
        if max(probabilities.values()) < min_confidence:
            fallbacks += 1
            covered += 1
            kept_tools += sum(counts.values())
            continue
        kept = set(select_domains(probabilities, coverage=coverage, max_domains=max_domains))
        kept |= set(event.get("pinned_domains") or [])
        covered += int(used <= kept)
        kept_tools += sum(count for domain, count in counts.items() if domain in kept)
    return {
        "coverage": coverage,
        "min_confidence": min_confidence,
        "decided": decided,
        "covered_rate": covered / decided if decided else None,
        "fallback_rate": fallbacks / decided if decided else None,
        "kept_tool_share": kept_tools / visible_tools if visible_tools else None,
    }


def summarize(
    events: list[dict[str, Any]],
    *,
    max_domains: int = 4,
    coverages: Iterable[float] = DEFAULT_COVERAGES,
    min_confidences: Iterable[float] = DEFAULT_MIN_CONFIDENCES,
) -> dict[str, Any]:
    statuses = Counter(str(event.get("status") or "") for event in events)
    latencies = sorted(int(event.get("latency_ms") or 0) for event in events if event.get("status") in {"ok", "low_confidence"})
    top_used = [event.get("top_domain_used") for event in events if event.get("top_domain_used") is not None]
    missed = Counter(domain for event in events for domain in event.get("missed_domains") or [])
    return {
        "events": len(events),
        "statuses": dict(statuses),
        "latency_ms_p50": latencies[len(latencies) // 2] if latencies else None,
        "latency_ms_p95": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None,
        "top_domain_used_rate": sum(top_used) / len(top_used) if top_used else None,
        "most_missed_domains": missed.most_common(5),
        "sweep": [
            replay_policy(events, coverage=coverage, min_confidence=min_confidence, max_domains=max_domains)
            for coverage in coverages
            for min_confidence in min_confidences
        ],
    }


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:5.1f}%"


def format_summary(summary: dict[str, Any]) -> str:
    lines = [
        f"shadow decisions: {summary['events']}  statuses: {summary['statuses']}",
        f"latency p50/p95: {summary['latency_ms_p50']} / {summary['latency_ms_p95']} ms",
        f"top domain was used: {_pct(summary['top_domain_used_rate'])}",
        f"most missed domains: {summary['most_missed_domains']}",
        "",
        "coverage  min_conf  covered  fallback  tools kept",
    ]
    for row in summary["sweep"]:
        lines.append(
            f"{row['coverage']:>8.2f}  {row['min_confidence']:>8.2f}  {_pct(row['covered_rate'])}"
            f"   {_pct(row['fallback_rate'])}     {_pct(row['kept_tool_share'])}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from app.agent.observability.recorder import DB_PATH

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DB_PATH, help="observability SQLite database")
    parser.add_argument("--limit", type=int, default=1000, help="most recent decisions to read")
    parser.add_argument("--max-domains", type=int, default=4)
    args = parser.parse_args(argv)
    if not args.db.exists():
        print(f"No observability database at {args.db}")
        return 1
    print(format_summary(summarize(load_shadow_events(args.db, limit=args.limit), max_domains=args.max_domains)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
