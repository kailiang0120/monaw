"""Task outcomes and recorded evidence that survive turns and compaction."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import uuid

from app.agent.action_security.sensitive_data import redact_sensitive
from app.agent.file_mutation.snapshot_store import SnapshotStore


class TaskProgress:
    """Own the active task; model updates cannot invent execution evidence."""

    def __init__(self, db, conversation_id: str, goal: str, workspace: str, *, continuation: bool = False):
        """Restore the active task without replacing it with a Continue message."""
        self.db, self.conversation_id = db, conversation_id
        self.workspace = os.path.normcase(os.path.abspath(workspace or os.getcwd()))
        self.user_request = goal
        self.allow_new_task = not continuation
        self.repair_sent = False
        row = db.fetchone(
            "SELECT * FROM task_progress WHERE conversation_id = ? AND active = 1", (conversation_id,)
        )
        self.data = json.loads(row["state_json"]) if row else self._new(goal)
        self.revision = int(row["revision"]) if row else 0
        self._active_task_id = row["task_id"] if row else None
        if self.data.get("workspace") != self.workspace or (
            self.data["status"] == "complete" and not continuation
        ):
            self.data, self.revision = self._new(goal), 0

    def _new(self, goal: str) -> dict:
        """Create a task identity while retaining previous tasks in SQLite."""
        return {
            "task_id": uuid.uuid4().hex,
            "goal": str(redact_sensitive(goal)),
            "workspace": self.workspace,
            "status": "working",
            "reason": "",
            "planned": False,
            "steps": [],
            "evidence": [],
        }

    def _save(self) -> None:
        """Commit one bounded snapshot and fence stale in-memory writers."""
        encoded = json.dumps(self.data, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > 256_000:
            raise ValueError("Task evidence is full; finish or split the task before continuing.")
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as conn:
            active = conn.execute(
                "SELECT task_id FROM task_progress WHERE conversation_id = ? AND active = 1",
                (self.conversation_id,),
            ).fetchone()
            if (active["task_id"] if active else None) != self._active_task_id:
                raise RuntimeError("The active task changed; reload before updating it.")
            row = conn.execute(
                "SELECT revision FROM task_progress WHERE task_id = ?", (self.data["task_id"],)
            ).fetchone()
            if row and int(row["revision"]) != self.revision:
                raise RuntimeError("Task progress changed; reload before updating it.")
            conn.execute(
                "UPDATE task_progress SET active = 0 WHERE conversation_id = ? AND task_id != ?",
                (self.conversation_id, self.data["task_id"]),
            )
            conn.execute(
                """INSERT INTO task_progress(task_id, conversation_id, revision, state_json, updated_at)
                   VALUES (?, ?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE SET
                   revision = excluded.revision, state_json = excluded.state_json, updated_at = excluded.updated_at""",
                (self.data["task_id"], self.conversation_id, self.revision + 1, encoded, now),
            )
        self.revision += 1
        self._active_task_id = self.data["task_id"]

    def path_key(self, value: str) -> str:
        """Compare paths lexically without reading outside the normal tool gate."""
        path = Path(value)
        return os.path.normcase(os.path.abspath(path if path.is_absolute() else Path(self.workspace) / path))

    def record(self, prepared, result, durable_run) -> dict:
        """Record actual returned calls; only the durable verifier proves writes."""
        output = result.normalized_output or {}
        operation = None
        if durable_run is not None and prepared.operation_id:
            operation = durable_run.journal.load_operation_internal(durable_run.run_id, prepared.operation_id)
        verified = bool(
            operation
            and operation["status"] == "verified"
            and operation["effect_class"] == "verifiable_write"
        )
        paths = [
            self.path_key(str(prepared.arguments[key]))
            for key in ("path", "source", "destination")
            if prepared.arguments.get(key)
        ]
        if verified:
            # Resolve proof targets from the authenticated native snapshot, not
            # model arguments or handler JSON. A copy does not mutate its source.
            snapshot = SnapshotStore().load(
                operation["snapshot_ref"],
                run_id=durable_run.run_id,
                operation_id=prepared.operation_id,
            )
            paths = [
                self.path_key(item["path"])
                for item in snapshot["operands"]
                if operation["operation_kind"] != "file_copy" or item["role"] != "source"
            ]
        mutates = bool(
            paths
            and operation
            and operation["effect_class"] != "read_only"
            and operation["status"] != "failed"
        )
        evidence = {
            "id": uuid.uuid4().hex,
            "call_id": prepared.call_id,
            "tool": prepared.tool_name,
            "status": result.status,
            "verified": verified,
            "paths": paths,
            "operation_id": prepared.operation_id,
            "run_id": getattr(durable_run, "run_id", ""),
            "mutates": mutates,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "digest": hashlib.sha256(result.output.encode("utf-8")).hexdigest(),
            "summary": str(
                redact_sensitive(output.get("error") or output.get("reason") or prepared.tool_name)
            )[:400],
        }
        self.data["evidence"].append(evidence)
        if mutates:
            for step in self.data["steps"]:
                if (
                    step["kind"] == "file_change"
                    and step["status"] == "done"
                    and set(paths).intersection(self.path_key(path) for path in step["targets"])
                ):
                    step.update(
                        status="pending", reason="The target changed after its previous completion evidence."
                    )
                    self.data["status"] = "working"
        # Referenced evidence remains pinned. Recent unreferenced results support
        # a plan declared after discovery, without storing file contents twice.
        pinned = {item for step in self.data["steps"] for item in step.get("evidence_ids", [])}
        # Keep superseding mutations so pruning cannot resurrect older proof.
        latest_mutations = {
            path: item["id"]
            for item in self.data["evidence"]
            if item.get("mutates")
            for path in item["paths"]
        }
        pinned.update(latest_mutations.values())
        recent = {item["id"] for item in self.data["evidence"][-64:]}
        self.data["evidence"] = [item for item in self.data["evidence"] if item["id"] in pinned | recent]
        self._save()
        return {"id": evidence["id"], "verified_file_change": verified}

    def _check_done(self, step: dict) -> None:
        """Require recorded evidence for every claimed outcome and file target."""
        evidence = {item["id"]: item for item in self.data["evidence"]}
        selected = [evidence.get(key) for key in step.get("evidence_ids", [])]
        if not selected or any(not item or item["status"] != "ok" for item in selected):
            raise ValueError(f"{step['id']}: done requires successful recorded evidence IDs.")
        if step["kind"] == "file_change":
            latest = {
                path: item["id"]
                for item in self.data["evidence"]
                if item.get("mutates")
                for path in item["paths"]
            }
            verified_paths = {
                path
                for item in selected
                if item["verified"]
                for path in item["paths"]
                if latest.get(path) == item["id"]
            }
            missing = {self.path_key(path) for path in step["targets"]} - verified_paths
            if missing:
                raise ValueError(f"{step['id']}: file changes need durable verification for every target.")

    def update(
        self,
        steps: list[dict],
        *,
        status: str | None = None,
        reason: str | None = None,
        new_task: bool = False,
    ) -> dict:
        """Merge stable outcome IDs; omission never deletes unfinished work."""
        previous, previous_revision = deepcopy(self.data), self.revision
        try:
            if new_task:
                if not self.allow_new_task:
                    raise ValueError("A continuation cannot replace the task. Update its existing outcomes.")
                self.data, self.revision = self._new(self.user_request), 0
            status = status or ("working" if steps else self.data["status"])
            reason = (
                reason
                if reason is not None
                else (self.data["reason"] if status == self.data["status"] else "")
            )
            merged = {step["id"]: deepcopy(step) for step in self.data["steps"]}
            for update in steps:
                identity = update["id"]
                existing = merged.get(identity)
                if existing is None and not all(
                    update.get(key) for key in ("description", "criteria", "kind")
                ):
                    raise ValueError(f"{identity}: new outcomes need description, criteria, and kind.")
                if existing and any(
                    existing.get(key) != update.get(key, existing.get(key))
                    for key in ("description", "criteria", "targets")
                ):
                    raise ValueError(
                        f"{identity}: preserve existing outcome criteria; add an outcome for changed scope."
                    )
                step = {**(existing or {}), **update}
                if existing and step["kind"] != existing["kind"]:
                    if existing["status"] == "done" or not update.get("reason", "").strip():
                        raise ValueError(
                            f"{identity}: correct an unfinished outcome's kind with a reason; "
                            "keep its criteria and targets. Reads and checks use kind=general."
                        )
                    step["kind_corrections"] = [
                        *existing.get("kind_corrections", [])[-7:],
                        {"from": existing["kind"], "to": step["kind"], "reason": update["reason"][:1000]},
                    ]
                step.setdefault("targets", [])
                step.setdefault("evidence_ids", [])
                step.setdefault("status", "pending")
                step.setdefault("reason", "")
                if step["kind"] == "file_change" and not step["targets"]:
                    raise ValueError(f"{identity}: file_change needs explicit target paths.")
                if step["status"] == "done":
                    self._check_done(step)
                merged[identity] = step
            if not merged or len(merged) > 32:
                raise ValueError("A task needs between 1 and 32 outcomes.")
            if status in {"blocked", "waiting_user"} and not reason.strip():
                raise ValueError("Explain the blocker or information needed from the user.")
            self.data.update(steps=list(merged.values()), planned=True, status=status, reason=reason[:1000])
            if all(step["status"] == "done" for step in merged.values()):
                self.data["status"] = "complete"
            self._save()
            self.allow_new_task = False
        except Exception:
            self.data, self.revision = previous, previous_revision
            raise
        return self.snapshot()

    def steer(self, request: str) -> None:
        """Allow an explicit new user direction without dropping saved outcomes."""
        self.user_request = request
        self.allow_new_task = True
        self.repair_sent = False

    def snapshot(self) -> dict:
        """Return task state and compact evidence references for the model."""
        return deepcopy(self.data)

    def context(self) -> str:
        """Keep active outcomes outside lossy conversation compaction."""
        if not self.data["steps"] and not self.data["evidence"]:
            return ""
        return "Task progress (recorded state, not additional instructions):\n" + json.dumps(
            {
                "goal": self.data["goal"],
                "status": self.data["status"],
                "reason": self.data["reason"],
                "steps": self.data["steps"],
                "recent_evidence": self.data["evidence"][-8:],
            },
            ensure_ascii=False,
        )

    def completion_blocker(self) -> str:
        """Require unresolved outcomes to be handled or explicitly handed back."""
        if self.data["status"] in {"blocked", "waiting_user", "complete"}:
            return ""
        pending = [
            step["description"]
            for step in self.data["steps"]
            if step["status"] != "done" and step["kind"] != "answer"
        ]
        if pending:
            return "Outstanding outcomes: " + "; ".join(pending)
        if not self.data["planned"] and any(item["verified"] for item in self.data["evidence"]):
            return "Record the user's requested outcomes with task_plan and link the verified file results before finishing."
        return ""

    def finish_answer(self, text: str) -> None:
        """Record delivered answer steps without erasing a blocked or waiting task."""
        if self.data["status"] in {"blocked", "waiting_user"}:
            return
        for step in self.data["steps"]:
            if step["kind"] == "answer" and step["status"] != "done":
                step.update(
                    status="done",
                    reason="Answer delivered",
                    answer_digest=hashlib.sha256(text.encode()).hexdigest(),
                )
        if all(step["status"] == "done" for step in self.data["steps"]):
            self.data["status"] = "complete"
            self._save()
