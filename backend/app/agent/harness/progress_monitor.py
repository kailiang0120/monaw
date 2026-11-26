"""Bound repeated observations that produce no new task information."""

from collections import OrderedDict
import hashlib
import json

_VOLATILE = {
    "task_evidence",
    "timestamp",
    "duration_ms",
    "elapsed_ms",
    "request_id",
    "trace_id",
    "generated_at",
}


def semantic_result(value):
    """Remove transport noise while preserving file and page state changes."""
    if isinstance(value, dict):
        return {key: semantic_result(item) for key, item in value.items() if key not in _VOLATILE}
    if isinstance(value, list):
        return [semantic_result(item) for item in value]
    return value


class ProgressMonitor:
    """Allow observation repeats, warn on a stall, then pause a bounded loop."""

    def __init__(self):
        """Keep a small recent-result cache rather than unbounded raw outputs."""
        self.seen = OrderedDict()
        self.stalled_rounds = 0
        self.redundant_calls = 0
        self.warned = False
        self.begin_round()

    def begin_round(self):
        """Collect progress across the complete batch, including parallel reads."""
        self.observed = 0
        self.changed = False
        self.polling = False

    def steer(self):
        """Give new user instructions a fresh recovery window, retaining metrics."""
        self.seen.clear()
        self.stalled_rounds = 0
        self.warned = False
        self.begin_round()

    def observe(self, name, arguments, output, status, tool):
        """Compare returned information; read permission is independent of progress."""
        metadata = (tool or {}).get("metadata", {}) or {}
        observation = (
            metadata.get("observation")
            or metadata.get("mutates_state") is False
            or metadata.get("internal_task_progress")
        )
        if not observation and status == "ok":
            # Actual action results can advance the task. Their effect/replay
            # safety remains owned by the durable journal and action guards.
            self.changed = True
            return
        try:
            value = json.loads(output)
        except (TypeError, ValueError):
            value = str(output).strip()
        if name == "task_plan" and isinstance(value, dict) and isinstance(value.get("task"), dict):
            value = {key: value["task"].get(key) for key in ("steps", "status", "reason")}
        value = semantic_result(value)
        # Rephrasing an empty search is still the same absence of results.
        empty_search = (
            ("search" in name or metadata.get("progress_search") is True)
            and isinstance(value, dict)
            and value.get("results") == []
        )
        if empty_search:
            value = {"results": []}
        identity = (
            name
            if empty_search or status != "ok" or name == "task_plan"
            else name + json.dumps(arguments, sort_keys=True, default=str)
        )
        digest = hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()
        key = hashlib.sha256((identity + digest).encode()).hexdigest()
        repeated = key in self.seen
        self.redundant_calls += int(repeated)
        self.changed = self.changed or not repeated
        self.observed += 1
        # Trusted descriptors opt status tools into a longer, still finite wait.
        self.polling = self.polling or metadata.get("progress_polling") is True
        self.seen[key] = None
        self.seen.move_to_end(key)
        while len(self.seen) > 128:
            self.seen.popitem(last=False)

    def finish_round(self):
        """Give one recovery opportunity, preserving bounded deliberate polling."""
        if self.changed:
            self.stalled_rounds = 0
            self.warned = False
            return "", False
        if not self.observed:
            return "", False
        self.stalled_rounds += 1
        warn_after, stop_after = (6, 12) if self.polling else (3, 6)
        if self.stalled_rounds >= stop_after:
            return (
                "Paused: repeated tool results provided no new information. Explain the blocker or choose a different approach before continuing.",
                True,
            )
        if self.stalled_rounds >= warn_after and not self.warned:
            self.warned = True
            return (
                "The recent tool results are unchanged. Use the results already available, try a meaningfully different approach, or explain what is blocking progress. Do not keep repeating the same observations.",
                False,
            )
        return "", False
