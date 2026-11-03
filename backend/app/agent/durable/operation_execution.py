"""Durable fences immediately around tool handler execution."""

from __future__ import annotations

import json
import threading
import uuid
from contextvars import ContextVar, Token
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
        self._pending_ticket_id = ""
        self._pending_ticket_status = ""
        self._active_approval_ticket_id = ""
        self._pending_approval_binding = ""
        self._approval_bindings: dict[str, str] = {}
        self._active_approval_binding = ""
        self._native_transient_grant_candidate = ""
        self._native_transient_grant_approvals: dict[str, str] = {}

    @property
    def effect_started(self) -> bool:
        return self._effect_started

    @property
    def trusted_pending_resume(self) -> bool:
        return self._pending_resume

    @property
    def approved_ticket_id(self) -> str:
        """Approval ticket whose exact payload is executing in this handler."""
        return self._active_approval_ticket_id

    @property
    def approved_binding_digest(self) -> str:
        """Private plan binding retained only in this live operation context."""
        return self._active_approval_binding

    @property
    def durable_workspace_dir(self) -> str:
        return str(_field(self.durable_run, "workspace_dir", "") or "")

    def can_promote_native_file_mutation(self) -> bool:
        """Whether this is one supported trusted native file operation."""
        return bool(
            self.trusted_builtin
            and self.operation_kind in {
                "file_write", "file_move", "file_copy", "file_delete", "file_mkdir", "file_mutation"
            }
            and self.tool_name in {
                "fs_write", "fs_append", "fs_patch", "fs_move", "fs_rename",
                "fs_copy", "fs_delete", "file_write", "file_append", "file_patch",
                "move", "rename", "copy", "delete",
            }
        )

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
                self._active_approval_ticket_id = (
                    self._pending_ticket_id
                    if self._pending_ticket_status == "pending_approval"
                    else ""
                )
                self._active_approval_binding = (
                    self._approval_bindings.get(self._active_approval_ticket_id, "")
                    if self._active_approval_ticket_id
                    else ""
                )
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
            self._active_approval_ticket_id = ""
            self._active_approval_binding = ""

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
            self._pending_ticket_id = (
                str(payload.get("ticket_id") or "") if self._pending_resume else ""
            )
            self._pending_ticket_status = (
                str(payload.get("status") or "") if self._pending_resume else ""
            )
            if self._pending_resume and self._pending_ticket_status == "pending_approval":
                if self._pending_approval_binding:
                    self._approval_bindings[self._pending_ticket_id] = self._pending_approval_binding
                self._pending_approval_binding = ""
                self._native_transient_grant_candidate = ""
            elif self._pending_resume:
                # An access grant continuation is not an approval of a plan.
                self._pending_approval_binding = ""
                self._clear_native_transient_grants()
            return self._pending_resume

    def bind_pending_approval_to_plan(self, binding_digest: str) -> None:
        """Bind the next native approval ticket to its private immutable plan."""
        if (
            not isinstance(binding_digest, str)
            or len(binding_digest) != 64
            or any(char not in "0123456789abcdef" for char in binding_digest)
        ):
            raise ValueError("binding_digest must be a SHA-256 digest")
        with self._lock:
            if current_operation_execution() is not self or not self.can_promote_native_file_mutation():
                raise OperationDispatchBlocked("unknown", "No native file approval context is active.")
            self._pending_approval_binding = binding_digest

    def note_native_transient_grant_candidate(self, binding_digest: str) -> None:
        """Remember an exact once-grant scope before its legacy gate consumes it."""
        if (
            not isinstance(binding_digest, str)
            or len(binding_digest) != 64
            or any(char not in "0123456789abcdef" for char in binding_digest)
        ):
            raise ValueError("binding_digest must be a SHA-256 digest")
        with self._lock:
            if current_operation_execution() is not self or not self.can_promote_native_file_mutation():
                raise OperationDispatchBlocked("unknown", "No native file grant context is active.")
            self._native_transient_grant_candidate = binding_digest

    def bind_native_transient_grant_to_approval(
        self, ticket_id: str, binding_digest: str
    ) -> bool:
        """Bind the consumed legacy grant to the mutation approval it produced."""
        with self._lock:
            if (
                current_operation_execution() is not self
                or not self.can_promote_native_file_mutation()
                or not ticket_id
                or self._native_transient_grant_candidate != binding_digest
            ):
                return False
            self._native_transient_grant_approvals[str(ticket_id)] = binding_digest
            self._native_transient_grant_candidate = ""
            return True

    def has_native_transient_grant_approval(
        self, ticket_id: str, binding_digest: str
    ) -> bool:
        """Check the live approved continuation against its private exact scope."""
        with self._lock:
            return bool(
                current_operation_execution() is self
                and self.can_promote_native_file_mutation()
                and ticket_id
                and self._active_approval_ticket_id == ticket_id
                and self._native_transient_grant_approvals.get(ticket_id) == binding_digest
            )

    def _clear_native_transient_grants(self) -> None:
        self._native_transient_grant_candidate = ""
        self._native_transient_grant_approvals.clear()

    def promote_native_file_mutation(
        self,
        *,
        snapshot_ref: str,
        input_summary: dict[str, Any],
        precondition_evidence: dict[str, Any],
    ) -> None:
        """Commit snapshot-backed verifiable classification before native I/O."""
        with self._lock:
            if current_operation_execution() is not self or not self.can_promote_native_file_mutation():
                raise OperationDispatchBlocked(
                    "unknown", "Only the active trusted native file operation can be promoted."
                )
            if not self._effect_started or self._status != "executing" or self.effect_class != "opaque_effect":
                raise OperationDispatchBlocked(
                    "external_effect_unknown", "The native file operation is not at its promotion boundary."
                )
            assert_dispatch_allowed(self.durable_run, self.lifecycle)
            self.journal.promote_operation_to_verifiable_write(
                self.run_id,
                self.operation_id,
                owner_instance_id=self.owner_instance_id,
                attempt=self.attempt,
                snapshot_ref=snapshot_ref,
                input_summary=input_summary,
                precondition_evidence=precondition_evidence,
            )
            self.effect_class = "verifiable_write"

    def assert_native_file_effect_allowed(self) -> None:
        """Recheck durable ownership/pause/review gates at the native boundary."""
        if current_operation_execution() is not self or not self.can_promote_native_file_mutation():
            raise OperationDispatchBlocked(
                "unknown", "Only the active trusted native file operation can mutate files."
            )
        if self._status != "executing" or self.effect_class != "verifiable_write":
            raise OperationDispatchBlocked(
                "external_effect_unknown", "The native file operation has no committed snapshot."
            )
        assert_dispatch_allowed(self.durable_run, self.lifecycle)

    def assert_native_file_preflight_allowed(self) -> None:
        """Recheck durable fences before staging a native file result."""
        if current_operation_execution() is not self or not self.can_promote_native_file_mutation():
            raise OperationDispatchBlocked(
                "unknown", "Only the active trusted native file operation can stage files."
            )
        if self._status != "executing":
            raise OperationDispatchBlocked(
                "external_effect_unknown", "The native file operation is no longer executing."
            )
        assert_dispatch_allowed(self.durable_run, self.lifecycle)

    def native_file_no_effect(self, reason_code: str = "operation_failed") -> None:
        """Settle a promoted native file operation when no filesystem effect ran."""
        with self._lock:
            if (
                current_operation_execution() is not self
                or not self.can_promote_native_file_mutation()
                or self.effect_class not in {"opaque_effect", "verifiable_write"}
            ):
                return
            if self._status != "executing":
                return
            self._transition(
                "failed",
                reason_code=reason_code,
                evidence={"verification_result": "unchanged"},
            )
            self._clear_native_transient_grants()

    def verify_native_file_mutation(self, verifier: object) -> dict[str, Any]:
        """Verify the private snapshot-backed row; never trust handler output."""
        with self._lock:
            if current_operation_execution() is not self or self.effect_class != "verifiable_write":
                raise OperationDispatchBlocked(
                    "external_effect_unknown", "No promoted native file operation is active."
                )
            if self._status != "executing":
                raise OperationDispatchBlocked(
                    "external_effect_unknown", "The native file operation is no longer executing."
                )
            load = getattr(self.journal, "load_operation_internal", None)
            verify = getattr(verifier, "verify", None)
            if not callable(load) or not callable(verify):
                raise TypeError("native file verifier is unavailable")
            private_operation = load(self.run_id, self.operation_id)
            if not isinstance(private_operation, dict):
                raise RuntimeError("private file operation evidence is unavailable")
            result = verify(private_operation)
            evidence = result if isinstance(result, dict) else {}
            verification = str(evidence.get("verification_result") or "unknown")
            if verification == "completed":
                self._transition("verified", reason_code="verified", evidence=evidence)
            elif verification == "unchanged":
                self._transition("failed", reason_code="operation_failed", evidence=evidence)
            else:
                self._transition("uncertain", reason_code="verification_failed", evidence=evidence)
                _block_run(self.run_id, self.lifecycle)
            return evidence

    def native_file_verification_unavailable(self) -> None:
        """Keep the run blocked when post-effect evidence could not be read."""
        with self._lock:
            if (
                current_operation_execution() is self
                and self._status == "executing"
                and self.effect_class == "verifiable_write"
            ):
                self._settle_uncertain("verification_failed")

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
            self._clear_native_transient_grants()

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
        self._clear_native_transient_grants()


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


_CURRENT_OPERATION_EXECUTION: ContextVar[OperationExecution | None] = ContextVar(
    "agent_current_operation_execution", default=None
)


def current_operation_execution() -> OperationExecution | None:
    return _CURRENT_OPERATION_EXECUTION.get()


def set_current_operation_execution(
    operation: OperationExecution | None,
) -> Token[OperationExecution | None]:
    return _CURRENT_OPERATION_EXECUTION.set(operation)


def reset_current_operation_execution(token: Token[OperationExecution | None]) -> None:
    _CURRENT_OPERATION_EXECUTION.reset(token)


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
