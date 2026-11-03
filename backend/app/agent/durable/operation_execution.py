"""Durable fences immediately around tool handler execution."""

from __future__ import annotations

import json
import threading
import uuid
from typing import Any

from app.agent.durable.models import EffectClass, OperationStatus
from app.agent.durable.operation_policy import classify_tool, is_trusted_preflight


class OperationDispatchBlocked(RuntimeError):
    """No tool handler may start because the run is paused or needs review."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _field(value: object, name: str, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _request_review(lifecycle: object | None, reason_code: str = "external_effect_unknown") -> None:
    request = getattr(lifecycle, "request_review", None)
    if callable(request):
        request(reason_code)


def _block_run(run_id: str, lifecycle: object | None) -> None:
    _request_review(lifecycle)


def assert_dispatch_allowed(
    durable_run: object | None,
    lifecycle: object | None = None,
    *,
    begin_run: bool = False,
) -> None:
    """Recheck pause, lease, owner, overlap and unresolved-operation fences.

    Call this before scheduling a tool and again inside the worker immediately
    before the callable. Existing operations already executing in parallel are
    allowed to settle; an uncertain write blocks any later effect dispatch.
    """
    if durable_run is None:
        return

    run_id = str(_field(durable_run, "run_id", "") or "")
    if not run_id:
        raise OperationDispatchBlocked("unknown", "Durable operation context is incomplete.")
    if bool(_field(durable_run, "lease_lost", False)):
        raise OperationDispatchBlocked("owner_conflict", "The durable run lease is no longer owned.")
    if bool(_field(durable_run, "pause_requested", False)) or bool(
        getattr(lifecycle, "pause_requested", False)
    ):
        raise OperationDispatchBlocked("pause_requested", "The run is pausing before the next tool action.")
    if bool(getattr(lifecycle, "review_requested", False)):
        raise OperationDispatchBlocked(
            "external_effect_unknown",
            "A previous tool action needs review before another effect can start.",
        )

    # DurableTurnRun transitions waiting_approval back to running only for a
    # trusted gate continuation. Ordinary dispatch must not clear another
    # sibling's approval wait.
    begin_dispatch = getattr(durable_run, "begin_dispatch", None)
    if begin_run and callable(begin_dispatch):
        begin_dispatch()

    # This guard checks the authoritative owner, lease, attempt, recovery
    # barrier, and workspace/conversation conflicts.
    from app.agent.durable.recovery import assert_run_dispatch_allowed

    assert_run_dispatch_allowed(durable_run)

    journal = _field(durable_run, "journal")
    if journal is None:
        raise OperationDispatchBlocked("unknown", "Durable journal is unavailable.")
    row = journal.db.fetchone(
        """SELECT 1 FROM run_operations WHERE run_id = ? AND effect_class != 'read_only'
           AND status IN ('uncertain', 'needs_review') LIMIT 1""",
        (run_id,),
    )
    if row is not None:
        raise OperationDispatchBlocked(
            "external_effect_unknown",
            "A previous tool action needs review before another effect can start.",
        )


class OperationExecution:
    """One operation identity reused only across its trusted gate continuation."""

    def __init__(
        self,
        *,
        durable_run: object,
        lifecycle: object | None,
        operation_id: str,
        tool_name: str,
        trusted_builtin: bool,
        journal_tool_name: str,
        operation_kind: str,
        effect_class: EffectClass,
    ) -> None:
        self.durable_run = durable_run
        self.lifecycle = lifecycle
        self.operation_id = operation_id
        self.tool_name = tool_name
        self.trusted_builtin = trusted_builtin
        self.journal_tool_name = journal_tool_name
        self.operation_kind = operation_kind
        self.effect_class = effect_class
        self.journal = _field(durable_run, "journal")
        self.run_id = str(_field(durable_run, "run_id", ""))
        self.owner_instance_id = str(_field(durable_run, "owner_instance_id", ""))
        self.attempt = int(_field(durable_run, "attempt", 0))
        self._lock = threading.RLock()
        self._status: OperationStatus = "prepared"
        self._effect_started = False
        self._pending_resume = False

    @property
    def effect_started(self) -> bool:
        return self._effect_started

    @property
    def trusted_pending_resume(self) -> bool:
        return self._pending_resume

    def prepare(self) -> None:
        # This commit must complete before ToolExecutor can schedule a worker.
        self.journal.prepare_operation(
            self.run_id,
            operation_id=self.operation_id,
            owner_instance_id=self.owner_instance_id,
            attempt=self.attempt,
            operation_kind=self.operation_kind,
            effect_class=self.effect_class,
            input_summary={},
            tool_name=self.journal_tool_name,
        )

    def _transition(
        self,
        to_status: OperationStatus,
        *,
        reason_code: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        self.journal.transition_operation(
            self.run_id,
            self.operation_id,
            to_status=to_status,
            owner_instance_id=self.owner_instance_id,
            attempt=self.attempt,
            reason_code=reason_code,
            evidence=evidence,
        )
        self._status = to_status

    def begin_effect(self) -> None:
        """Commit `executing` after locks/cancellation and immediately before entry."""
        with self._lock:
            if self._effect_started:
                # A second entry is the trusted gate continuation. Revalidate
                # fences but keep the original operation ID and executing row.
                if not self._pending_resume:
                    raise OperationDispatchBlocked(
                        "external_effect_unknown",
                        "This operation cannot be replayed without a trusted preflight result.",
                    )
                assert_dispatch_allowed(self.durable_run, self.lifecycle, begin_run=True)
                self._pending_resume = False
                return
            assert_dispatch_allowed(self.durable_run, self.lifecycle)
            if self._status == "prepared":
                self._transition("authorized")
            if self._status != "authorized":
                raise OperationDispatchBlocked(
                    "unknown", "This operation is no longer dispatchable."
                )
            self._transition("executing")
            self._effect_started = True

    def note_pending_preflight(self, output: object) -> bool:
        """Accept a pending gate only when a known native preflight is safe."""
        with self._lock:
            payload = _payload(output if isinstance(output, str) else None)
            read_only_gate = (
                self.effect_class == "read_only"
                and self.trusted_builtin
                and payload.get("status") in {"pending_approval", "pending_access_grant"}
                and isinstance(payload.get("ticket_id"), str)
                and bool(payload.get("ticket_id"))
            )
            self._pending_resume = bool(self._effect_started and (
                read_only_gate
                or is_trusted_preflight(
                    self.tool_name, output, trusted_builtin=self.trusted_builtin
                )
            ))
            return self._pending_resume

    def preflight_rejected(self, reason_code: str = "approval_rejected") -> None:
        """Record a known no-effect denial from a trusted native gate."""
        with self._lock:
            if not self._pending_resume or self._status != "executing":
                return
            self._transition(
                "failed",
                reason_code=reason_code,
                evidence={"verification_result": "unchanged"},
            )
            self._pending_resume = False

    def _settle_uncertain(self, reason_code: str = "external_effect_unknown") -> None:
        self._transition(
            "uncertain",
            reason_code=reason_code,
            evidence={"verification_result": "unknown"},
        )
        _block_run(self.run_id, self.lifecycle)

    def settle(self, output: str | None = None, *, cancelled: bool = False) -> None:
        """Close read-only work; preserve uncertainty for unverified effects."""
        with self._lock:
            try:
                self._settle(output, cancelled=cancelled)
            except Exception:
                if self._effect_started and self.effect_class != "read_only":
                    _block_run(self.run_id, self.lifecycle)
                raise

    def _settle(self, output: str | None, *, cancelled: bool) -> None:
        if self._status in {"verified", "failed", "cancelled", "returned", "uncertain", "needs_review"}:
            return

        if not self._effect_started:
            if self._status in {"prepared", "authorized"}:
                self._transition("cancelled", reason_code="skipped")
            return

        result_status = _result_status(output)
        if self.effect_class == "read_only":
            if cancelled or result_status in {
                "error", "failed", "blocked", "denied", "cancelled",
                "pending_approval", "pending_access_grant",
            }:
                self._transition("failed", reason_code="operation_failed")
            else:
                self._transition("verified", reason_code="verified")
            return

        if result_status in {"pending_approval", "pending_access_grant"}:
            self._settle_uncertain("external_effect_unknown")
            return

        if cancelled or result_status in {"error", "failed", "blocked", "denied", "cancelled"}:
            if self._pending_resume and not cancelled:
                self._transition(
                    "failed",
                    reason_code="approval_rejected",
                    evidence={"verification_result": "unchanged"},
                )
                self._pending_resume = False
            elif self._pending_resume and cancelled:
                self._transition(
                    "failed",
                    reason_code="cancelled",
                    evidence={"verification_result": "unchanged"},
                )
                self._pending_resume = False
            else:
                self._settle_uncertain(
                    "timeout" if _payload_reason(output) in {"tool_timeout", "timeout"} else "operation_failed"
                )
            return

        # A returned opaque action is not a claim that its effect was
        # verified; the recovery verifier may inspect it after interruption.
        self._transition(
            "returned",
            reason_code="unknown",
            evidence={"verification_result": "unknown"},
        )


def _payload(output: str | None) -> dict[str, Any]:
    try:
        value = json.loads(output) if isinstance(output, str) else {}
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _payload_reason(output: str | None) -> str:
    return str(_payload(output).get("reason_code") or "")


def _result_status(output: str | None) -> str:
    payload = _payload(output)
    return str(payload.get("status") or "ok").lower()


def prepare_tool_operation(
    durable_run: object | None,
    *,
    tool_name: str,
    trusted_builtin: bool = False,
    lifecycle: object | None = None,
) -> OperationExecution | None:
    """Persist a prepared operation before any handler work is scheduled."""
    if durable_run is None:
        return None
    assert_dispatch_allowed(durable_run, lifecycle)
    policy = classify_tool(tool_name, trusted_builtin=trusted_builtin)
    operation_id = f"op-{uuid.uuid4().hex}"
    operation = OperationExecution(
        durable_run=durable_run,
        lifecycle=lifecycle,
        operation_id=operation_id,
        tool_name=tool_name,
        trusted_builtin=trusted_builtin,
        journal_tool_name=policy.journal_tool_name,
        operation_kind=policy.operation_kind,
        effect_class=policy.effect_class,
    )
    operation.prepare()
    return operation
