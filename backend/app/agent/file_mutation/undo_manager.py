"""Conservative, journaled Undo for verified native file mutations."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agent.controller_policy import resolve_permission
from app.agent.durable.journal import Journal
from app.agent.durable.models import (
    IdempotencyConflictError,
    InvalidTransitionError,
    JournalConflictError,
    OperationNotFoundError,
    RevisionConflictError,
)
from app.agent.file_mutation.fingerprint import FileFingerprint, FingerprintError, canonical_path, fingerprint_path
from app.agent.file_mutation.diff_engine import unified_diff_preview
from app.agent.file_mutation.planner import MutationPlan, PathState, PlanOperand, build_plan
from app.agent.file_mutation.snapshot_store import SnapshotStore, SnapshotStoreError, _safe_root_identity, get_snapshot_store
from app.agent.file_mutation.verifier import FileMutationVerifier, _state_matches
from app.agent.permission_actions import ActionType


class UndoError(RuntimeError):
    reason_code = "undo_unavailable"


class UndoConflictError(UndoError):
    reason_code = "undo_conflict"


class UndoUnavailableError(UndoError):
    reason_code = "undo_unavailable"


class UndoPermissionError(UndoError):
    reason_code = "permission_denied"


class UndoNeedsReviewError(UndoError):
    reason_code = "external_effect_unknown"


class UndoIdempotencyConflictError(UndoError):
    reason_code = "idempotency_conflict"


PermissionCheck = Callable[[ActionType, str], object]
OwnerProvider = Callable[[], str]
_UNDO_OPERATION_PREFIX = "undo-"
_UNDO_STEP_PREFIX = "undo-step-"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


def _default_permission_check(action: ActionType, path: str) -> object:
    # Do not reuse a once/session grant from the original operation.
    return resolve_permission(
        action, target_path=path, allow_session_grants=False, consume_once_grants=False
    )


def _default_owner_provider() -> str:
    from app.agent.durable.recovery import get_owner_instance_id

    return get_owner_instance_id()


def _decision_allowed(decision: object) -> bool:
    if isinstance(decision, bool):
        return decision
    if isinstance(decision, Mapping):
        return bool(
            decision.get("allowed") is True
            and decision.get("blocked") is not True
            and decision.get("requires_access_grant") is not True
            and decision.get("requires_confirmation") is not True
        )
    return bool(
        getattr(decision, "allowed", False) is True
        and getattr(decision, "blocked", False) is not True
        and getattr(decision, "requires_access_grant", False) is not True
        and getattr(decision, "requires_confirmation", False) is not True
    )


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}{digest}"


def _identity(value: object) -> tuple[int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    if any(isinstance(part, bool) or not isinstance(part, int) for part in value):
        return None
    return int(value[0]), int(value[1])


def _absent_state(path: str, parent_identity: tuple[int, int]) -> PathState:
    return PathState.metadata(
        path, exists=False, kind="absent", size=0, parent_identity=parent_identity
    )


def _set_fd_times(descriptor: int, *, atime_ns: int | None, modified_ns: int) -> None:
    if os.name != "nt":
        os.utime(descriptor, ns=(atime_ns if atime_ns is not None else modified_ns, modified_ns))
        return
    import msvcrt
    from ctypes import wintypes

    def to_filetime(value: int):
        ticks = int(value) // 100 + 116444736000000000
        if ticks < 0:
            raise ValueError("file timestamp is out of range")
        return wintypes.FILETIME(ticks & 0xFFFFFFFF, (ticks >> 32) & 0xFFFFFFFF)

    access_time = to_filetime(atime_ns) if atime_ns is not None else None
    write_time = to_filetime(modified_ns)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_file_time = kernel32.SetFileTime
    set_file_time.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    set_file_time.restype = wintypes.BOOL
    if not set_file_time(
        msvcrt.get_osfhandle(descriptor),
        None,
        ctypes.byref(access_time) if access_time is not None else None,
        ctypes.byref(write_time),
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _delete_exact_file(
    path: Path, expected: PathState, *, confirmation_granted: bool = False
) -> bool:
    """Delete a file only when the filesystem can bind deletion to its identity.

    Windows uses P3's exclusive, handle-bound delete implementation. POSIX has
    no conditional unlink-by-inode primitive, so cleanup fails closed there.
    """
    if os.name != "nt":
        return False
    try:
        from app.skills.filesystem.file_ops import _p3_delete_regular_file

        if confirmation_granted:
            _p3_delete_regular_file(path, expected, allow_confirmed_mutation=True)
        else:
            _p3_delete_regular_file(path, expected)
    except Exception:
        # A changed identity, denied permission, or unsupported filesystem
        # leaves the artifact for review instead of unlinking by path.
        return False
    return True


def _atomic_rename_no_replace(
    source: Path,
    destination: Path,
    *,
    expected_source_parent: tuple[int, int] | None = None,
    expected_destination_parent: tuple[int, int] | None = None,
) -> None:
    """Same-volume rename that refuses an occupied destination leaf."""
    source_now = fingerprint_path(source)
    destination_now = fingerprint_path(destination)
    source_parent = source_now.parent_identity
    destination_parent = destination_now.parent_identity
    if (
        (expected_source_parent is not None and source_parent != expected_source_parent)
        or (expected_destination_parent is not None and destination_parent != expected_destination_parent)
    ):
        raise UndoConflictError("a parent directory changed before the file rename")
    if os.name == "nt":
        from ctypes import wintypes

        move_file_ex = ctypes.windll.kernel32.MoveFileExW
        move_file_ex.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move_file_ex.restype = wintypes.BOOL
        if not move_file_ex(str(source), str(destination), 0x00000008):  # WRITE_THROUGH, without REPLACE_EXISTING
            raise ctypes.WinError()
        if fingerprint_path(destination).parent_identity != destination_parent:
            raise UndoConflictError("destination parent changed during the file rename")
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, "renameat2"):
        raise UndoUnavailableError("atomic no-replace rename is unavailable on this filesystem")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    source_directory = os.open(source.parent, directory_flags)
    destination_directory = os.open(destination.parent, directory_flags)
    try:
        source_stat = os.fstat(source_directory)
        destination_stat = os.fstat(destination_directory)
        if (
            (int(source_stat.st_dev), int(source_stat.st_ino)) != source_parent
            or (int(destination_stat.st_dev), int(destination_stat.st_ino)) != destination_parent
        ):
            raise UndoConflictError("a parent directory changed before the file rename")
        renameat2 = libc.renameat2
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            source_directory,
            os.fsencode(source.name),
            destination_directory,
            os.fsencode(destination.name),
            1,
        )  # RENAME_NOREPLACE
        if result:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(destination))
        os.fsync(source_directory)
        if destination_directory != source_directory:
            os.fsync(destination_directory)
    finally:
        os.close(source_directory)
        os.close(destination_directory)


class UndoManager:
    """Apply an exact, currently authorized inverse of a verified P3 file effect.

    Each Undo has a durable parent row plus one or more normal snapshot-backed
    file-operation rows. Pending steps are reconciled read-only at startup and
    are never automatically replayed.
    """

    def __init__(
        self,
        journal: Journal,
        store: SnapshotStore | None = None,
        verifier: FileMutationVerifier | None = None,
        *,
        permission_check: PermissionCheck | None = None,
        owner_provider: OwnerProvider | None = None,
    ) -> None:
        self.journal = journal
        self.store = store or get_snapshot_store()
        self.permission_check = permission_check or _default_permission_check
        self.verifier = verifier or FileMutationVerifier(
            self.store, read_policy=lambda path: self.permission_check(ActionType.READ, path)
        )
        self.owner_provider = owner_provider or _default_owner_provider

    def undo(
        self,
        run_id: str,
        operation_id: str,
        *,
        principal_id: str | None = None,
        idempotency_key: str | None = None,
        expected_revision: int | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        source = self.journal.load_operation_internal(run_id, operation_id)
        if source is None:
            raise OperationNotFoundError(operation_id)
        if principal_id is None:
            run = self.journal.load_run_internal(run_id)
            principal_id = str(run.get("owner_principal_id") or "") if run is not None else ""
        if not isinstance(principal_id, str) or not principal_id.strip():
            raise ValueError("principal_id is required")
        if idempotency_key is None:
            idempotency_key = "undo:" + hashlib.sha256(f"{run_id}\0{operation_id}".encode()).hexdigest()
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 256:
            raise ValueError("a bounded Idempotency-Key is required")
        if (
            source["effect_class"] != "verifiable_write"
            or source["status"] != "verified"
            or source["operation_kind"] not in {"file_write", "file_move", "file_delete"}
            or not isinstance(source.get("snapshot_ref"), str)
        ):
            raise UndoUnavailableError("operation has no verified, supported file effect")
        try:
            original = self.store.load(
                str(source["snapshot_ref"]), run_id=run_id, operation_id=operation_id
            )
        except SnapshotStoreError as exc:
            raise UndoUnavailableError("the original file snapshot is unavailable") from exc
        if not self._snapshot_is_live(original):
            raise UndoUnavailableError("the file snapshot has expired")
        operands = original.get("operands")
        if not isinstance(operands, list) or not operands or any(not isinstance(item, dict) for item in operands):
            raise UndoUnavailableError("the original file snapshot is malformed")
        self._authorize_source_inverse(
            str(source["operation_kind"]), operands, confirmation_granted=confirmed
        )
        principal_hash = hashlib.sha256(principal_id.encode("utf-8")).hexdigest()
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        payload_hash = _digest({
            "action": "undo",
            "run_id": run_id,
            "operation_id": operation_id,
            "expected_revision": expected_revision,
        })
        try:
            prior = self.journal.lookup_undo_idempotency(
                run_id,
                operation_id,
                principal_hash=principal_hash,
                idempotency_key_hash=key_hash,
                payload_hash=payload_hash,
            )
        except IdempotencyConflictError as exc:
            raise UndoIdempotencyConflictError("Undo idempotency key was reused with a different payload") from exc
        if prior is not None:
            return self._existing_claim_result(run_id, operation_id, prior)

        try:
            observed = self._fingerprint_original_after(operands)
        except FingerprintError as exc:
            raise UndoConflictError("file state could not be safely rechecked") from exc
        if not self._matches_original_after(original, observed):
            raise UndoConflictError("file state no longer matches the verified operation post-state")

        step_count = self._expected_step_count(str(source["operation_kind"]), operands)
        owner_id = self.owner_provider()
        undo_id = _stable_id(_UNDO_OPERATION_PREFIX, run_id, operation_id, principal_hash, key_hash)
        completed_steps: list[str] = []
        try:
            claim = self.journal.claim_undo_operation(
                run_id,
                operation_id,
                undo_operation_id=undo_id,
                principal_hash=principal_hash,
                idempotency_key_hash=key_hash,
                payload_hash=payload_hash,
                owner_instance_id=owner_id,
                expected_steps=step_count,
                expected_revision=expected_revision,
            )
        except IdempotencyConflictError as exc:
            raise UndoConflictError("Undo request conflicts with an existing claim or idempotency key") from exc
        except InvalidTransitionError as exc:
            raise UndoUnavailableError(str(exc)) from exc
        if not claim["created"]:
            return self._existing_claim_result(run_id, operation_id, claim["operation"])

        try:
            self.journal.transition_undo_operation(
                run_id, undo_id, owner_instance_id=owner_id, to_status="authorized",
                evidence={"verification_result": "not_checked"},
            )
            self.journal.transition_undo_operation(
                run_id, undo_id, owner_instance_id=owner_id, to_status="executing",
                evidence={"verification_result": "not_checked"},
            )
            for index in range(step_count):
                step_id = _stable_id(_UNDO_STEP_PREFIX, undo_id, str(index))
                spec = self._build_step_spec(source, original, observed, index=index)
                stage_path: Path | None = None
                plan: MutationPlan | None = None
                try:
                    self._authorize_step(spec, confirmation_granted=confirmed)
                    self.journal.prepare_undo_step(
                        run_id, undo_id, step_operation_id=step_id,
                        operation_kind=spec["operation_kind"],
                        input_summary={"target_count": len(spec["paths"])},
                        precondition_evidence={"verification_result": "not_checked"},
                    )
                    plan, before_bytes, stage_path, effect_data = self._plan_step(
                        run_id=run_id, undo_operation_id=undo_id,
                        step_operation_id=step_id, spec=spec,
                    )
                    try:
                        snapshot_ref = self.store.capture(
                            plan, before_bytes, run_id=run_id, operation_id=step_id,
                            authorized_roots=spec["authorized_roots"],
                        )
                    except Exception as exc:
                        if not self._cleanup_stage(stage_path, plan):
                            raise UndoNeedsReviewError(
                                "staged inverse artifact could not be removed safely"
                            ) from exc
                        raise
                except UndoNeedsReviewError:
                    self._cancel_unstarted_steps(run_id, undo_id, owner_id)
                    self._mark_parent_needs_review(run_id, undo_id, owner_id)
                    raise
                except (UndoPermissionError, UndoConflictError):
                    if completed_steps:
                        self._mark_parent_needs_review(run_id, undo_id, owner_id)
                    else:
                        self._fail_parent_without_effect(run_id, undo_id, owner_id)
                    raise
                except (SnapshotStoreError, FingerprintError, OSError, ValueError):
                    if completed_steps:
                        self._mark_parent_needs_review(run_id, undo_id, owner_id)
                    else:
                        self._fail_parent_without_effect(run_id, undo_id, owner_id)
                    raise
                self.journal.set_undo_step_snapshot(run_id, undo_id, step_id, snapshot_ref)
                self.journal.transition_undo_operation(
                    run_id, step_id, owner_instance_id=owner_id, to_status="authorized",
                    evidence={"snapshot_available": True, "verification_result": "not_checked"},
                )
                self.journal.transition_undo_operation(
                    run_id, step_id, owner_instance_id=owner_id, to_status="executing",
                    evidence={"snapshot_available": True, "verification_result": "not_checked"},
                )
                # Do not catch BaseException: process-level interruption leaves
                # an executing row for startup recovery instead of replaying.
                try:
                    self._apply_inverse_step(
                        operation_kind=spec["operation_kind"], plan=plan,
                        payload=effect_data.get("payload"), stage_path=stage_path,
                        step_operation_id=step_id,
                        target_modified_ns=effect_data.get("target_modified_ns"),
                        confirmation_granted=confirmed,
                    )
                except Exception as exc:
                    evidence = self.verifier.verify(
                        self.journal.load_operation_internal(run_id, step_id) or {}
                    )
                    self._record_step_verification(run_id, step_id, owner_id, evidence)
                    if isinstance(exc, UndoNeedsReviewError):
                        self._mark_parent_needs_review(run_id, undo_id, owner_id)
                        raise
                    if evidence.get("verification_result") == "completed":
                        completed_steps.append(step_id)
                        continue
                    if evidence.get("verification_result") == "unchanged" and not completed_steps:
                        self._fail_parent_without_effect(run_id, undo_id, owner_id)
                        raise UndoConflictError("inverse file effect did not start") from exc
                    self._mark_parent_needs_review(run_id, undo_id, owner_id)
                    raise UndoNeedsReviewError("inverse filesystem effect needs inspection") from exc

                evidence = self.verifier.verify(self.journal.load_operation_internal(run_id, step_id) or {})
                if evidence.get("verification_result") != "completed":
                    self._record_step_verification(run_id, step_id, owner_id, evidence)
                    if evidence.get("verification_result") == "unchanged" and not completed_steps:
                        self.journal.transition_undo_operation(
                            run_id, undo_id, owner_instance_id=owner_id, to_status="failed",
                            reason_code="undo_conflict", evidence={"verification_result": "unchanged"},
                        )
                        raise UndoConflictError("inverse precondition changed before the file effect")
                    self._mark_parent_needs_review(run_id, undo_id, owner_id)
                    raise UndoNeedsReviewError("inverse filesystem effect could not be verified")
                self._record_step_verification(run_id, step_id, owner_id, evidence)
                completed_steps.append(step_id)

            self.journal.transition_undo_operation(
                run_id, undo_id, owner_instance_id=owner_id, to_status="verified",
                reason_code="verified",
                evidence={"verification_result": "completed", "target_count": len(operands)},
            )
            return {
                "run_id": run_id, "operation_id": operation_id,
                "undo_operation_id": undo_id, "status": "undone",
                "reason_code": None,
                "evidence": {"verification_result": "completed", "target_count": len(operands)},
            }
        except (UndoConflictError, UndoPermissionError):
            if completed_steps:
                self._mark_parent_needs_review(run_id, undo_id, owner_id)
            else:
                self._fail_parent_without_effect(run_id, undo_id, owner_id)
            raise
        except UndoNeedsReviewError:
            self._mark_parent_needs_review(run_id, undo_id, owner_id)
            raise
        except UndoUnavailableError as exc:
            if completed_steps:
                self._mark_parent_needs_review(run_id, undo_id, owner_id)
                raise UndoNeedsReviewError("Undo stopped with an unresolved inverse") from exc
            self._fail_parent_without_effect(run_id, undo_id, owner_id)
            raise
        except (SnapshotStoreError, FingerprintError, OSError, ValueError) as exc:
            if completed_steps:
                self._mark_parent_needs_review(run_id, undo_id, owner_id)
                raise UndoNeedsReviewError("Undo stopped with an unresolved inverse") from exc
            self._fail_parent_without_effect(run_id, undo_id, owner_id)
            raise UndoConflictError("Undo could not safely prepare its inverse") from exc

    def preview(self, run_id: str, operation_id: str) -> dict[str, Any]:
        """Return a bounded preview and private fingerprint binding for one inverse.

        This method never claims a journal operation or writes to the filesystem.
        Confirmation may satisfy a policy confirmation requirement, but it
        cannot satisfy a blocked path or an access grant.
        """
        source = self.journal.load_operation_internal(run_id, operation_id)
        if source is None:
            raise OperationNotFoundError(operation_id)
        if (
            source["effect_class"] != "verifiable_write"
            or source["status"] != "verified"
            or source["operation_kind"] not in {"file_write", "file_move", "file_delete"}
            or not isinstance(source.get("snapshot_ref"), str)
        ):
            raise UndoUnavailableError("operation has no verified, supported file effect")
        try:
            original = self.store.load(
                str(source["snapshot_ref"]), run_id=run_id, operation_id=operation_id
            )
        except SnapshotStoreError as exc:
            raise UndoUnavailableError("the original file snapshot is unavailable") from exc
        if not self._snapshot_is_live(original):
            raise UndoUnavailableError("the file snapshot has expired")
        operands = original.get("operands")
        if not isinstance(operands, list) or not operands or any(not isinstance(item, dict) for item in operands):
            raise UndoUnavailableError("the original file snapshot is malformed")

        self._authorize_source_inverse(
            str(source["operation_kind"]), operands, confirmation_granted=True
        )
        try:
            observed = self._fingerprint_original_after(operands)
        except FingerprintError as exc:
            raise UndoConflictError("file state could not be safely rechecked") from exc
        if not self._matches_original_after(original, observed):
            raise UndoConflictError("file state no longer matches the verified operation post-state")

        files = self._preview_files(str(source["operation_kind"]), original, observed)
        fingerprint = _digest({
            "snapshot_ref": source["snapshot_ref"],
            "binding_digest": original.get("binding_digest"),
            "operands": {
                path: self._fingerprint_binding(value)
                for path, value in sorted(observed.items())
            },
        })
        return {"files": files, "fingerprint_digest": fingerprint}

    @staticmethod
    def _fingerprint_binding(value: FileFingerprint) -> dict[str, Any]:
        return {
            "path": value.path,
            "exists": value.exists,
            "kind": value.kind,
            "size": value.size,
            "sha256": value.sha256,
            "identity": list(value.identity) if value.identity is not None else None,
            "parent_identity": list(value.parent_identity),
            "modified_ns": value.modified_ns,
            "created_ns": value.created_ns,
        }

    @staticmethod
    def _preview_state(value: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "exists": bool(value.get("exists")),
            "kind": str(value.get("kind") or ("file" if value.get("exists") else "absent")),
            "size": int(value.get("size") or 0),
        }

    def _preview_files(
        self,
        operation_kind: str,
        original: Mapping[str, Any],
        observed: Mapping[str, FileFingerprint],
    ) -> list[dict[str, Any]]:
        operands = original["operands"]
        rows: list[dict[str, Any]] = []

        def add_row(
            operand: Mapping[str, Any],
            current: FileFingerprint,
            after_state: Mapping[str, Any],
            after_bytes: bytes | None,
            action: str,
        ) -> None:
            path = str(operand["path"])
            diff = unified_diff_preview(
                current.data,
                after_bytes,
                fromfile=path,
                tofile=f"{path} after Undo",
            )
            rows.append({
                "path": path,
                "role": str(operand.get("role") or "target"),
                "action": action,
                "before": self._preview_state({
                    "exists": current.exists,
                    "kind": current.kind,
                    "size": current.size,
                }),
                "after": self._preview_state(after_state),
                "preview": diff.to_dict(),
            })

        if operation_kind == "file_write":
            target = operands[0]
            path = str(target["path"])
            prior = target["before"]
            prior_bytes = self.store.before_bytes(original, path) if prior["exists"] else None
            add_row(
                target,
                observed[path],
                prior,
                prior_bytes,
                "restore" if prior["exists"] else "remove",
            )
            return rows

        if operation_kind == "file_delete":
            target = operands[0]
            path = str(target["path"])
            add_row(
                target,
                observed[path],
                target["before"],
                self.store.before_bytes(original, path),
                "restore",
            )
            return rows

        if operation_kind == "file_move":
            source = next(item for item in operands if str(item.get("role") or "").lower() == "source")
            destination = next(
                item for item in operands
                if str(item.get("role") or "").lower() in {"destination", "dest", "target"}
            )
            source_path, destination_path = str(source["path"]), str(destination["path"])
            moved_bytes = observed[destination_path].data
            if moved_bytes is None:
                raise UndoUnavailableError("the moved file bytes are unavailable for preview")
            add_row(source, observed[source_path], source["before"], moved_bytes, "restore")
            prior_destination = destination["before"]
            destination_bytes = (
                self.store.before_bytes(original, destination_path)
                if prior_destination["exists"] else None
            )
            add_row(
                destination,
                observed[destination_path],
                prior_destination,
                destination_bytes,
                "restore" if prior_destination["exists"] else "remove",
            )
            return rows

        raise UndoUnavailableError("inverse preview is unavailable for this operation")

    @staticmethod
    def _snapshot_is_live(snapshot: Mapping[str, Any]) -> bool:
        try:
            expires = datetime.fromisoformat(str(snapshot.get("expires_at") or ""))
            return bool(expires.tzinfo and expires.astimezone(timezone.utc) > datetime.now(timezone.utc))
        except (TypeError, ValueError):
            return False

    def _permission_allowed(
        self, action: ActionType, path: str, *, confirmation_granted: bool = False
    ) -> bool:
        try:
            decision = self.permission_check(action, path)
            if _decision_allowed(decision):
                return True
            if not confirmation_granted:
                return False
            if isinstance(decision, Mapping):
                return bool(
                    decision.get("allowed") is True
                    and decision.get("blocked") is not True
                    and decision.get("requires_access_grant") is not True
                    and decision.get("requires_confirmation") is True
                )
            return bool(
                getattr(decision, "allowed", False) is True
                and getattr(decision, "blocked", False) is not True
                and getattr(decision, "requires_access_grant", False) is not True
                and getattr(decision, "requires_confirmation", False) is True
            )
        except Exception:
            return False

    def _authorize_source_inverse(
        self,
        operation_kind: str,
        operands: list[dict[str, Any]],
        *,
        confirmation_granted: bool = False,
    ) -> None:
        actions: set[tuple[ActionType, str]] = set()
        for item in operands:
            path = str(item.get("path") or "")
            if not path:
                raise UndoUnavailableError("file snapshot operand has no path")
            actions.add((ActionType.READ, path))
            actions.add((ActionType.MUTATE, path))
        if operation_kind == "file_write" and len(operands) == 1 and operands[0].get("before", {}).get("exists") is not True:
            path = str(operands[0]["path"])
            if not confirmation_granted:
                actions.discard((ActionType.MUTATE, path))
                actions.add((ActionType.DELETE, path))
        for action, path in actions:
            if not self._permission_allowed(action, path, confirmation_granted=confirmation_granted):
                raise UndoPermissionError("current file permissions do not allow Undo")

    def _authorize_step(
        self, spec: Mapping[str, Any], *, confirmation_granted: bool = False
    ) -> None:
        action = ActionType.DELETE if spec["operation_kind"] == "file_delete" else ActionType.MUTATE
        if (
            confirmation_granted
            and spec["operation_kind"] == "file_delete"
            and spec["source"].get("operation_kind") == "file_write"
            and spec["roles"]["target"].get("before", {}).get("exists") is not True
        ):
            action = ActionType.MUTATE
        for path in spec["paths"]:
            if not self._permission_allowed(ActionType.READ, path) or not self._permission_allowed(
                action, path, confirmation_granted=confirmation_granted
            ):
                raise UndoPermissionError("current file permissions do not allow this inverse step")

    def _fingerprint_operand(self, operand: Mapping[str, Any], *, include_bytes: bool) -> FileFingerprint:
        path_value, root_value = operand.get("path"), operand.get("authorized_root")
        if not isinstance(path_value, str) or str(canonical_path(path_value)) != path_value:
            raise UndoUnavailableError("file snapshot path is invalid")
        if not isinstance(root_value, str) or str(canonical_path(root_value)) != root_value:
            raise UndoUnavailableError("file snapshot scope is invalid")
        path, root = canonical_path(path_value), canonical_path(root_value)
        try:
            if path != root and not path.is_relative_to(root):
                raise UndoUnavailableError("file snapshot escaped its saved scope")
        except (OSError, RuntimeError, ValueError) as exc:
            raise UndoUnavailableError("file snapshot scope is invalid") from exc
        if operand.get("authorized_root_kind") == "directory":
            try:
                root_identity = list(_safe_root_identity(root))
            except (OSError, SnapshotStoreError, ValueError) as exc:
                raise UndoUnavailableError("saved authorized directory is unavailable") from exc
            if root_identity != operand.get("authorized_root_identity"):
                raise UndoUnavailableError("saved authorized directory identity changed")
        elif operand.get("authorized_root_kind") != "exact_path" or root != path:
            raise UndoUnavailableError("saved authorized file scope is invalid")
        if not self._permission_allowed(ActionType.READ, str(path)):
            raise UndoPermissionError("current read permission is required for Undo")
        return fingerprint_path(path, include_bytes=include_bytes)

    def _fingerprint_original_after(self, operands: list[dict[str, Any]]) -> dict[str, FileFingerprint]:
        return {
            str(item["path"]): self._fingerprint_operand(item, include_bytes=True)
            for item in operands
        }

    @staticmethod
    def _matches_original_after(
        original: Mapping[str, Any], observed: Mapping[str, FileFingerprint]
    ) -> bool:
        operands = original.get("operands")
        if not isinstance(operands, list) or not operands:
            return False
        return all(
            isinstance(item, Mapping)
            and str(item.get("path") or "") in observed
            and _state_matches(observed[str(item["path"])], item.get("after", {}))
            for item in operands
        )

    @staticmethod
    def _expected_step_count(operation_kind: str, operands: list[dict[str, Any]]) -> int:
        if operation_kind == "file_move":
            destination = next(
                (item for item in operands if str(item.get("role") or "").lower() in {"destination", "dest", "target"}),
                None,
            )
            if destination is None:
                raise UndoUnavailableError("move snapshot lacks its destination")
            return 2 if destination.get("before", {}).get("exists") is True else 1
        return 1

    def _build_step_spec(
        self,
        source: Mapping[str, Any],
        original: Mapping[str, Any],
        observed: Mapping[str, FileFingerprint],
        *,
        index: int,
    ) -> dict[str, Any]:
        kind, operands = str(source["operation_kind"]), original["operands"]
        if kind == "file_write":
            target = operands[0]
            return {
                "operation_kind": "file_write" if target["before"]["exists"] else "file_delete",
                "paths": [str(target["path"])], "roles": {"target": target},
                "authorized_roots": [target["authorized_root"]],
                "source": source, "original": original, "observed": observed, "step_index": index,
            }
        if kind == "file_delete":
            target = operands[0]
            return {
                "operation_kind": "file_write", "paths": [str(target["path"])],
                "roles": {"target": target}, "authorized_roots": [target["authorized_root"]],
                "source": source, "original": original, "observed": observed, "step_index": index,
            }
        source_operand = next(item for item in operands if str(item.get("role") or "").lower() == "source")
        destination_operand = next(
            item for item in operands
            if str(item.get("role") or "").lower() in {"destination", "dest", "target"}
        )
        if index == 0:
            return {
                "operation_kind": "file_move",
                "paths": [str(destination_operand["path"]), str(source_operand["path"])],
                "roles": {"source": destination_operand, "destination": source_operand},
                "authorized_roots": [destination_operand["authorized_root"], source_operand["authorized_root"]],
                "source": source, "original": original, "observed": observed, "step_index": index,
            }
        return {
            "operation_kind": "file_write", "paths": [str(destination_operand["path"])],
            "roles": {"target": destination_operand}, "authorized_roots": [destination_operand["authorized_root"]],
            "source": source, "original": original, "observed": observed, "step_index": index,
        }

    def _plan_step(
        self,
        *,
        run_id: str,
        undo_operation_id: str,
        step_operation_id: str,
        spec: Mapping[str, Any],
    ) -> tuple[MutationPlan, dict[str, bytes], Path | None, dict[str, Any]]:
        kind = str(spec["operation_kind"])
        original = spec["original"]
        source_kind = str(spec["source"]["operation_kind"])
        role_map = spec["roles"]
        before_bytes: dict[str, bytes] = {}
        stage_path: Path | None = None
        effect_data: dict[str, Any] = {}

        if kind == "file_move":
            source_operand = role_map["source"]
            destination_operand = role_map["destination"]
            source_path, destination_path = str(source_operand["path"]), str(destination_operand["path"])
            source_now = self._fingerprint_operand(source_operand, include_bytes=False)
            destination_now = self._fingerprint_operand(destination_operand, include_bytes=False)
            if not _state_matches(source_now, source_operand["after"]) or not _state_matches(
                destination_now, destination_operand["after"]
            ):
                raise UndoConflictError("move source or destination changed before Undo")
            if not source_now.exists or destination_now.exists:
                raise UndoConflictError("move source is absent or its destination is occupied")
            source_before = PathState.from_fingerprint(source_now)
            source_after = _absent_state(source_path, source_now.parent_identity)
            destination_before = PathState.from_fingerprint(destination_now)
            destination_after = PathState(
                path=destination_path,
                exists=True,
                kind="file",
                size=source_now.size,
                sha256=source_now.sha256,
                identity=source_now.identity,
                modified_ns=source_now.modified_ns,
                created_ns=source_now.created_ns,
                parent_identity=destination_now.parent_identity,
            )
            plan = build_plan(
                "move_file",
                {"undo_operation_id": undo_operation_id, "step": spec["step_index"]},
                [
                    PlanOperand(source_path, "source", source_before, source_after, "absent"),
                    PlanOperand(destination_path, "destination", destination_before, destination_after, "replaced"),
                ],
                read_authorized=True,
            )
        elif kind == "file_delete":
            target = role_map["target"]
            path = str(target["path"])
            current = self._fingerprint_operand(target, include_bytes=True)
            if not _state_matches(current, target["after"]) or not current.exists or current.data is None:
                raise UndoConflictError("created file changed before Undo")
            before_bytes[path] = current.data
            plan = build_plan(
                "delete_file",
                {"undo_operation_id": undo_operation_id, "step": spec["step_index"]},
                [
                    PlanOperand(
                        path,
                        "target",
                        PathState.from_fingerprint(current),
                        _absent_state(path, current.parent_identity),
                        "absent",
                    )
                ],
                before_bytes=current.data,
                read_authorized=True,
            )
        elif kind == "file_write" and source_kind == "file_write" and role_map["target"]["before"]["exists"]:
            target = role_map["target"]
            path = str(target["path"])
            current = self._fingerprint_operand(target, include_bytes=True)
            if not _state_matches(current, target["after"]) or current.data is None:
                raise UndoConflictError("written file changed before Undo")
            restore = self.store.before_bytes(original, path)
            if restore is None:
                raise UndoUnavailableError("the original file bytes are unavailable")
            before_bytes[path] = current.data
            expected_after = PathState.from_bytes(
                path,
                restore,
                identity=current.identity,
                modified_ns=target["before"].get("modified_ns"),
                # Replaced files cannot regain the old file identity/creation
                # time. Content and last-write time are restorable.
                created_ns=None,
                parent_identity=current.parent_identity,
            )
            plan = build_plan(
                "write_file",
                {"undo_operation_id": undo_operation_id, "step": spec["step_index"]},
                [PlanOperand(path, "target", PathState.from_fingerprint(current), expected_after, "same")],
                before_bytes=current.data,
                after_bytes=restore,
                read_authorized=True,
            )
            effect_data.update(payload=restore, target_modified_ns=target["before"].get("modified_ns"))
        elif kind == "file_write":
            target = role_map["target"]
            path = str(target["path"])
            current = self._fingerprint_operand(target, include_bytes=False)
            if current.exists:
                raise UndoConflictError("restore destination is occupied")
            if source_kind == "file_delete":
                restore = self.store.before_bytes(original, path)
                expected_parent = _identity(target["after"].get("parent_identity"))
            elif source_kind == "file_move":
                # Step zero has moved the original source back. The displaced
                # destination leaf must now be empty under its original parent.
                restore = self.store.before_bytes(original, path)
                expected_parent = _identity(target["before"].get("parent_identity"))
            else:
                raise UndoUnavailableError("inverse file creation is unsupported")
            if restore is None:
                raise UndoUnavailableError("the original file bytes are unavailable")
            if expected_parent is None or current.parent_identity != expected_parent:
                raise UndoConflictError("restore destination parent changed")
            stage_path, staged = self._stage_new_file(
                step_operation_id, Path(path), restore, target_modified_ns=target["before"].get("modified_ns")
            )
            expected_after = PathState.from_bytes(
                path,
                restore,
                identity=staged.identity,
                modified_ns=target["before"].get("modified_ns"),
                created_ns=None,
                parent_identity=current.parent_identity,
            )
            plan = build_plan(
                "write_file",
                {"undo_operation_id": undo_operation_id, "step": spec["step_index"]},
                [PlanOperand(path, "target", PathState.from_fingerprint(current), expected_after, "replaced")],
                after_bytes=restore,
                read_authorized=True,
            )
            effect_data.update(payload=restore, target_modified_ns=target["before"].get("modified_ns"))
        else:
            raise UndoUnavailableError("inverse file operation is not supported")
        return plan, before_bytes, stage_path, effect_data

    @staticmethod
    def _stage_new_file(
        step_operation_id: str,
        target: Path,
        data: bytes,
        *,
        target_modified_ns: object,
    ) -> tuple[Path, FileFingerprint]:
        stage_name = f".monaw-undo-{hashlib.sha256(step_operation_id.encode()).hexdigest()[:24]}.tmp"
        stage = target.parent / stage_name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(stage, flags, 0o600)
        try:
            view = memoryview(data)
            offset = 0
            while offset < len(view):
                offset += os.write(descriptor, view[offset : offset + 64 * 1024])
            if isinstance(target_modified_ns, int) and not isinstance(target_modified_ns, bool):
                _set_fd_times(
                    descriptor,
                    atime_ns=getattr(os.fstat(descriptor), "st_atime_ns", None),
                    modified_ns=target_modified_ns,
                )
            os.fsync(descriptor)
        except BaseException as exc:
            try:
                failed_stage = fingerprint_path(stage, include_bytes=True)
            except (FingerprintError, OSError):
                failed_stage = None
            finally:
                os.close(descriptor)
            if failed_stage is not None and failed_stage.exists and not _delete_exact_file(
                stage, PathState.from_fingerprint(failed_stage)
            ):
                raise UndoNeedsReviewError(
                    "failed staging artifact could not be removed safely"
                ) from exc
            raise
        else:
            os.close(descriptor)
        return stage, fingerprint_path(stage)

    def _apply_inverse_step(
        self,
        *,
        operation_kind: str,
        plan: MutationPlan,
        payload: bytes | None,
        stage_path: Path | None,
        step_operation_id: str,
        target_modified_ns: object,
        confirmation_granted: bool = False,
    ) -> None:
        """Perform one fenced step; kept as a seam for crash-injection tests."""
        operands = plan.operands
        if operation_kind == "file_move":
            source, destination = Path(operands[0].path), Path(operands[1].path)
            _atomic_rename_no_replace(
                source,
                destination,
                expected_source_parent=operands[0].before.parent_identity,
                expected_destination_parent=operands[1].before.parent_identity,
            )
            moved = fingerprint_path(destination)
            if moved.identity != operands[0].before.identity or moved.sha256 != operands[0].before.sha256:
                _atomic_rename_no_replace(
                    destination,
                    source,
                    expected_source_parent=operands[1].before.parent_identity,
                    expected_destination_parent=operands[0].before.parent_identity,
                )
                raise UndoConflictError("move source changed at the filesystem boundary")
            return
        target, before = Path(operands[0].path), operands[0].before
        if operation_kind == "file_delete":
            self._remove_exact(
                target, before, step_operation_id,
                confirmation_granted=confirmation_granted,
            )
            return
        if operation_kind == "file_write" and before.exists:
            if payload is None:
                raise UndoUnavailableError("inverse bytes are unavailable")
            self._write_existing_exact(target, before, payload, target_modified_ns)
            return
        if operation_kind == "file_write" and stage_path is not None:
            _atomic_rename_no_replace(
                stage_path,
                target,
                expected_source_parent=operands[0].before.parent_identity,
                expected_destination_parent=operands[0].before.parent_identity,
            )
            created = fingerprint_path(target)
            expected = operands[0].after
            if created.identity != expected.identity or created.sha256 != expected.sha256:
                raise UndoConflictError("restored file did not match its staged identity")
            return
        raise UndoUnavailableError("inverse file operation lacks a safe execution plan")

    @staticmethod
    def _write_existing_exact(target: Path, expected: PathState, data: bytes, modified_ns: object) -> None:
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(target, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or int(getattr(opened, "st_nlink", 1)) != 1
                or (int(opened.st_dev), int(opened.st_ino)) != expected.identity
                or int(opened.st_size) != expected.size
            ):
                raise UndoConflictError("file identity changed before Undo write")
            os.lseek(descriptor, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            if hashlib.sha256(b"".join(chunks)).hexdigest() != expected.sha256:
                raise UndoConflictError("file content changed before Undo write")
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            view = memoryview(data)
            offset = 0
            while offset < len(view):
                offset += os.write(descriptor, view[offset : offset + 64 * 1024])
            os.ftruncate(descriptor, len(data))
            if isinstance(modified_ns, int) and not isinstance(modified_ns, bool):
                _set_fd_times(
                    descriptor,
                    atime_ns=getattr(opened, "st_atime_ns", None),
                    modified_ns=modified_ns,
                )
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _remove_exact(
        target: Path,
        expected: PathState,
        step_operation_id: str,
        *,
        confirmation_granted: bool = False,
    ) -> None:
        if os.name != "nt":
            raise UndoNeedsReviewError(
                "this platform cannot safely bind deletion to the verified file identity"
            )
        # Delete the authorized leaf directly with P3's exclusive handle-bound
        # primitive. This keeps current permission checks attached to the exact
        # file path covered by the server-side Undo confirmation; no temporary
        # sibling path is introduced into the authorization scope.
        if not _delete_exact_file(
            target, expected, confirmation_granted=confirmation_granted
        ):
            raise UndoNeedsReviewError(
                f"{step_operation_id}: file could not be deleted by its verified identity"
            )

    def _record_step_verification(
        self, run_id: str, step_id: str, owner_id: str, evidence: Mapping[str, Any]
    ) -> None:
        result = evidence.get("verification_result")
        if result == "completed":
            self.journal.transition_undo_operation(
                run_id, step_id, owner_instance_id=owner_id, to_status="verified",
                reason_code="verified", evidence=dict(evidence),
            )
        elif result == "unchanged":
            self.journal.transition_undo_operation(
                run_id, step_id, owner_instance_id=owner_id, to_status="failed",
                reason_code="undo_conflict", evidence=dict(evidence),
            )
        else:
            self.journal.transition_undo_operation(
                run_id, step_id, owner_instance_id=owner_id, to_status="uncertain",
                reason_code="external_effect_unknown", evidence={"verification_result": "unknown"},
            )
            self.journal.transition_undo_operation(
                run_id, step_id, owner_instance_id=owner_id, to_status="needs_review",
                reason_code="external_effect_unknown", evidence={"verification_result": "unknown"},
            )

    @staticmethod
    def _cleanup_stage(stage_path: Path | None, plan: MutationPlan | None) -> bool:
        if stage_path is None or plan is None or len(plan.operands) != 1:
            return True
        expected = plan.operands[0].after
        try:
            current = fingerprint_path(stage_path, include_bytes=True)
        except (OSError, FingerprintError):
            return False
        if not current.exists:
            return True
        if (
            current.identity != expected.identity
            or current.sha256 != expected.sha256
            or current.size != expected.size
            or current.parent_identity != expected.parent_identity
        ):
            return False
        return _delete_exact_file(stage_path, PathState.from_fingerprint(current))

    @staticmethod
    def cleanup_recovered_artifact(
        step: Mapping[str, Any],
        *,
        store: SnapshotStore,
        permission_check: PermissionCheck | None = None,
    ) -> bool:
        """Remove a known staging/quarantine leaf after read-only verification.

        The path is derived from the journaled child operation id and snapshot
        operand. Identity, parent identity, digest and current DELETE permission
        are checked before any cleanup. Returns False when cleanup is uncertain.
        """
        reference = step.get("snapshot_ref")
        if not isinstance(reference, str):
            return True
        try:
            snapshot = store.load(
                reference,
                run_id=str(step["run_id"]),
                operation_id=str(step["operation_id"]),
            )
        except (SnapshotStoreError, KeyError):
            return False
        operation_kind = str(step.get("operation_kind") or "")
        if operation_kind != "file_delete" and operation_kind != "file_write":
            return True
        operands = snapshot.get("operands")
        if not isinstance(operands, list) or len(operands) != 1 or not isinstance(operands[0], Mapping):
            return False
        operand = operands[0]
        target = canonical_path(str(operand.get("path") or ""))
        step_id = str(step.get("operation_id") or "")
        if operation_kind != "file_delete" and not (
            operation_kind == "file_write" and operand.get("before", {}).get("exists") is not True
        ):
            return True
        policy = permission_check or _default_permission_check
        if not _decision_allowed(policy(ActionType.DELETE, str(target))):
            return False
        artifact: Path | None = None
        expected: Mapping[str, Any] | None = None
        if operation_kind == "file_delete":
            target_now = fingerprint_path(target)
            if not _state_matches(target_now, operand.get("after", {})):
                return False
            artifact = target.parent / (
                f".monaw-undo-remove-{hashlib.sha256(step_id.encode()).hexdigest()[:24]}.tmp"
            )
            expected = operand.get("before")
        elif operation_kind == "file_write" and operand.get("before", {}).get("exists") is not True:
            target_now = fingerprint_path(target)
            if not _state_matches(target_now, operand.get("before", {})):
                return False
            artifact = target.parent / f".monaw-undo-{hashlib.sha256(step_id.encode()).hexdigest()[:24]}.tmp"
            expected = operand.get("after")
        else:
            return True
        try:
            artifact_value = os.lstat(artifact)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if (
            not stat.S_ISREG(artifact_value.st_mode)
            or int(getattr(artifact_value, "st_nlink", 1)) != 1
            or int(getattr(artifact_value, "st_file_attributes", 0)) & _FILE_ATTRIBUTE_REPARSE_POINT
        ):
            return False
        try:
            observed = fingerprint_path(artifact, include_bytes=True)
        except FingerprintError:
            return False
        if not isinstance(expected, Mapping) or (
            list(observed.identity or ()) != expected.get("identity")
            or observed.size != expected.get("size")
            or observed.sha256 != expected.get("sha256")
            or list(observed.parent_identity) != expected.get("parent_identity")
        ):
            return False
        if not _delete_exact_file(artifact, PathState.from_fingerprint(observed)):
            return False
        try:
            if os.name != "nt":
                directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                directory = os.open(target.parent, directory_flags)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except OSError:
            return False
        return True

    def _fail_parent_without_effect(self, run_id: str, undo_id: str, owner_id: str) -> None:
        steps = self.journal.load_undo_steps(run_id, undo_id)
        if any(step.get("status") == "verified" for step in steps):
            self._mark_parent_needs_review(run_id, undo_id, owner_id)
            return
        for step in steps:
            if step.get("status") not in {"prepared", "authorized"}:
                continue
            try:
                self.journal.transition_undo_operation(
                    run_id,
                    str(step["operation_id"]),
                    owner_instance_id=owner_id,
                    to_status="cancelled",
                    reason_code="undo_conflict",
                    evidence={"verification_result": "unchanged"},
                )
            except JournalConflictError:
                pass
        parent = self.journal.load_operation_internal(run_id, undo_id) or {}
        status = str(parent.get("status") or "")
        try:
            if status in {"prepared", "authorized"}:
                self.journal.transition_undo_operation(
                    run_id, undo_id, owner_instance_id=owner_id, to_status="cancelled",
                    reason_code="undo_conflict", evidence={"verification_result": "unchanged"},
                )
            elif status in {"executing", "uncertain", "needs_review"}:
                self.journal.transition_undo_operation(
                    run_id, undo_id, owner_instance_id=owner_id, to_status="failed",
                    reason_code="undo_conflict", evidence={"verification_result": "unchanged"},
                )
        except JournalConflictError:
            pass

    def _mark_parent_needs_review(self, run_id: str, undo_id: str, owner_id: str) -> None:
        self._cancel_unstarted_steps(run_id, undo_id, owner_id)
        parent = self.journal.load_operation_internal(run_id, undo_id) or {}
        status = str(parent.get("status") or "")
        try:
            if status == "executing":
                self.journal.transition_undo_operation(
                    run_id, undo_id, owner_instance_id=owner_id, to_status="uncertain",
                    reason_code="external_effect_unknown", evidence={"verification_result": "unknown"},
                )
                status = "uncertain"
            if status == "uncertain":
                self.journal.transition_undo_operation(
                    run_id, undo_id, owner_instance_id=owner_id, to_status="needs_review",
                    reason_code="external_effect_unknown", evidence={"verification_result": "unknown"},
                )
        except JournalConflictError:
            pass

    def _cancel_unstarted_steps(self, run_id: str, undo_id: str, owner_id: str) -> None:
        for step in self.journal.load_undo_steps(run_id, undo_id):
            if step.get("status") not in {"prepared", "authorized"}:
                continue
            try:
                self.journal.transition_undo_operation(
                    run_id,
                    str(step["operation_id"]),
                    owner_instance_id=owner_id,
                    to_status="cancelled",
                    reason_code="external_effect_unknown",
                    evidence={"verification_result": "not_checked"},
                )
            except JournalConflictError:
                pass

    @staticmethod
    def _existing_claim_result(run_id: str, source_id: str, operation: Mapping[str, Any]) -> dict[str, Any]:
        undo_id, status = str(operation["operation_id"]), str(operation["status"])
        if status == "verified":
            return {
                "run_id": run_id, "operation_id": source_id, "undo_operation_id": undo_id,
                "status": "undone", "reason_code": None,
                "evidence": {"verification_result": "completed"},
            }
        if status == "needs_review":
            return {
                "run_id": run_id, "operation_id": source_id, "undo_operation_id": undo_id,
                "status": "needs_review", "reason_code": "external_effect_unknown",
                "evidence": {"verification_result": "unknown"},
            }
        if status in {"prepared", "authorized", "executing", "uncertain"}:
            return {
                "run_id": run_id, "operation_id": source_id, "undo_operation_id": undo_id,
                "status": "in_progress", "reason_code": "undo_in_progress",
                "evidence": {"verification_result": "not_checked"},
            }
        raise UndoConflictError("the existing Undo claim did not complete")
