"""Startup recovery and process ownership proofs for durable Runs.

A database lease expiry alone never authorizes recovery. Recovery requires a
recognized per-process lock marker whose OS lock is no longer held. The marker
path is derived from a strict generated identifier under the fixed runtime
directory; identifiers from the database are never treated as paths.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from app.agent.database import get_db
from app.agent.durable.journal import Journal
from app.agent.runtime_paths import RUNTIME_DIR, WORKSPACE_DIR

logger = logging.getLogger(__name__)

_OWNER_ID_RE = re.compile(r"^owner-v1-[0-9a-f]{32}$")
_MARKER_NAME = "monaw-agent-owner-lock-v1"
_WINDOWS_LOCK_OFFSET = 4096
_OWNER_PROOF_RETRIES = 3
_OWNER_PROOF_RETRY_SECONDS = 0.05
_OWNER_LOCK_DIR = RUNTIME_DIR / "owner_locks"
_ACTIVE_RUN_STATUSES = ("running", "waiting_approval", "pausing", "interrupted", "needs_review")
_UNCERTAIN_WRITE_STATUSES = frozenset({"executing", "uncertain", "needs_review"})
_LOCAL_OWNER_IDS: set[str] = set()
_LOCAL_OWNER_IDS_LOCK = threading.Lock()

OwnerProof = Literal["live", "dead", "unknown"]
ReadOnlyVerifier = Callable[[dict[str, Any]], Mapping[str, object] | None]


class OwnerIdentityError(RuntimeError):
    """The process could not establish a durable owner proof."""


class DispatchConflictError(RuntimeError):
    """A durable run cannot dispatch while another run owns its scope."""


def _lock_file_path(owner_instance_id: str, *, lock_dir: Path) -> Path | None:
    """Derive a marker path only from our generated identifier namespace."""
    if not _OWNER_ID_RE.fullmatch(owner_instance_id):
        return None
    return lock_dir / f"{owner_instance_id}.lock"


def _try_os_lock(handle) -> bool:
    """Return True on lock acquisition, False only when another process holds it."""
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            # The marker is read from bytes [0, 256). Lock beyond that range so
            # another process can still prove the owner live without colliding
            # with Windows byte-range read locking.
            os.lseek(handle.fileno(), _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK, 13, 36}:
                return False
            raise

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise


def _unlock_os_lock(handle) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(handle.fileno(), _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class OwnerProcessIdentity:
    """One random, safe identity backed by a lock held for process lifetime."""

    def __init__(self, lock_dir: Path = _OWNER_LOCK_DIR) -> None:
        self.lock_dir = Path(lock_dir)
        self.owner_instance_id = ""
        self._handle = None
        self._lock = threading.Lock()

    def acquire(self) -> str:
        with self._lock:
            if self._handle is not None and self.owner_instance_id:
                return self.owner_instance_id
            try:
                self.lock_dir.mkdir(parents=True, exist_ok=True)
                if self.lock_dir.is_symlink() or not self.lock_dir.is_dir():
                    raise OwnerIdentityError("owner lock directory is not a regular directory")
                if os.name != "nt":
                    self.lock_dir.chmod(0o700)
            except OSError as exc:
                raise OwnerIdentityError("could not prepare owner lock directory") from exc

            for _ in range(5):
                owner_id = f"owner-v1-{uuid.uuid4().hex}"
                path = _lock_file_path(owner_id, lock_dir=self.lock_dir)
                assert path is not None
                try:
                    handle = path.open("x+b", buffering=0)
                except FileExistsError:
                    continue
                except OSError as exc:
                    raise OwnerIdentityError("could not create owner lock marker") from exc
                try:
                    handle.write(f"{_MARKER_NAME}\n{owner_id}\n".encode("ascii"))
                    os.lseek(handle.fileno(), _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
                    handle.write(b"\0")
                    handle.flush()
                    if os.name != "nt":
                        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
                    if not _try_os_lock(handle):
                        raise OwnerIdentityError("new owner lock marker was already locked")
                except BaseException:
                    handle.close()
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    raise
                self.owner_instance_id = owner_id
                self._handle = handle
                with _LOCAL_OWNER_IDS_LOCK:
                    _LOCAL_OWNER_IDS.add(owner_id)
                return owner_id
            raise OwnerIdentityError("could not allocate an owner lock identifier")

    def close(self) -> None:
        """Release the lock; production keeps it until process exit."""
        with self._lock:
            handle, self._handle = self._handle, None
            if handle is not None:
                with _LOCAL_OWNER_IDS_LOCK:
                    _LOCAL_OWNER_IDS.discard(self.owner_instance_id)
                try:
                    _unlock_os_lock(handle)
                finally:
                    handle.close()


_PROCESS_IDENTITY = OwnerProcessIdentity()


def get_owner_instance_id() -> str:
    """Return this process's stable, safe durable owner identifier."""
    return _PROCESS_IDENTITY.acquire()


def owner_process_proof(owner_instance_id: str, *, lock_dir: Path | None = None) -> OwnerProof:
    """Check the generated marker and OS lock; unknown evidence fails closed."""
    root = Path(lock_dir) if lock_dir is not None else _OWNER_LOCK_DIR
    path = _lock_file_path(owner_instance_id, lock_dir=root)
    if path is None:
        return "unknown"
    with _LOCAL_OWNER_IDS_LOCK:
        if owner_instance_id in _LOCAL_OWNER_IDS:
            return "live"
    try:
        if path.is_symlink():
            return "unknown"
        file_stat = path.stat()
        if not stat.S_ISREG(file_stat.st_mode):
            return "unknown"
        if os.name == "nt" and file_stat.st_size < _WINDOWS_LOCK_OFFSET + 1:
            return "unknown"
        with path.open("r+b", buffering=0) as handle:
            expected = f"{_MARKER_NAME}\n{owner_instance_id}\n".encode("ascii")
            marker = handle.read(len(expected))
            if marker != expected:
                return "unknown"
            acquired = _try_os_lock(handle)
            if not acquired:
                return "live"
            _unlock_os_lock(handle)
            return "dead"
    except FileNotFoundError:
        return "unknown"
    except (OSError, UnicodeError):
        return "unknown"


def _parse_expiry(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _operation_rows(journal: Journal, run_id: str) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    after_sequence = 0
    while True:
        page = journal.list_operations(run_id, limit=500, after_sequence=after_sequence)
        if not page:
            return operations
        for operation in page:
            internal = journal.load_operation_internal(run_id, str(operation["operation_id"]))
            if internal is not None:
                operations.append(internal)
        after_sequence = int(page[-1]["sequence"])
        if len(page) < 500:
            return operations


def _run_read_only_verifier(
    journal: Journal,
    *,
    run_id: str,
    owner_instance_id: str,
    attempt: int,
    operations: list[dict[str, Any]],
    verifier: ReadOnlyVerifier | None,
) -> int:
    """Apply only explicit read-only verification evidence; never replay work."""
    if verifier is None:
        return 0
    verified = 0
    for operation in operations:
        if (
            operation["effect_class"] != "verifiable_write"
            or operation["status"] not in {"executing", "uncertain", "needs_review"}
            or int(operation["attempt"]) != attempt
        ):
            continue
        try:
            evidence = verifier(dict(operation))
            if not isinstance(evidence, Mapping):
                continue
            result = evidence.get("verification_result")
            if result == "completed":
                journal.transition_operation(
                    run_id,
                    str(operation["operation_id"]),
                    to_status="verified",
                    owner_instance_id=owner_instance_id,
                    attempt=attempt,
                    reason_code="verified",
                    evidence=dict(evidence),
                )
                verified += 1
            elif result == "unchanged":
                journal.transition_operation(
                    run_id,
                    str(operation["operation_id"]),
                    to_status="failed",
                    owner_instance_id=owner_instance_id,
                    attempt=attempt,
                    reason_code="safe_to_retry",
                    evidence=dict(evidence),
                )
                verified += 1
        except Exception as exc:
            # A verifier is read-only by contract. A failed check leaves the
            # operation unresolved and never authorizes a retry.
            logger.warning(
                "durable recovery verifier failed run=%s operation=%s error_type=%s",
                run_id,
                operation["operation_id"],
                type(exc).__name__,
            )
    return verified


def _can_pause_recovered_run(journal: Journal, run: Mapping[str, Any]) -> bool:
    """Only expose Resume when its user checkpoint is reconstructable and safe."""
    with journal.db.transaction(immediate=False) as conn:
        row = conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (str(run["run_id"]),)).fetchone()
        return bool(row is not None and journal._has_safe_resume_input(conn, row))


def _cleanup_file_mutation_snapshots(journal: Journal) -> dict[str, int]:
    """Prune expired orphans while retaining every active/recoverable ref."""
    from app.agent.file_mutation.snapshot_store import SnapshotStore

    rows = journal.db.fetchall(
        """SELECT DISTINCT o.snapshot_ref, o.status AS operation_status, r.status AS run_status
           FROM run_operations AS o JOIN agent_runs AS r ON r.run_id = o.run_id
           WHERE o.snapshot_ref IS NOT NULL"""
    )
    keep_run_states = frozenset(
        {"running", "waiting_approval", "pausing", "interrupted", "needs_review", "paused"}
    )
    terminal_operation_states = frozenset({"verified", "failed", "cancelled", "returned"})
    referenced = {
        str(row["snapshot_ref"])
        for row in rows
        if str(row["run_status"]) in keep_run_states
        or str(row["operation_status"]) not in terminal_operation_states
    }
    return SnapshotStore().cleanup(referenced)


def recover_startup_runs(
    *,
    journal: Journal | None = None,
    recovery_owner_instance_id: str | None = None,
    read_only_verifier: ReadOnlyVerifier | None = None,
    owner_proof: Callable[[str], OwnerProof] | None = None,
    page_size: int = 100,
) -> dict[str, int]:
    """Handoff durable runs only after proving the old process is gone.

    Opaque effects and any other unresolved writes move to ``needs_review``.
    They are never replayed here. ``read_only_verifier`` is injectable for
    deterministic recovery tests and may only inspect external state.
    """
    if journal is None:
        journal = Journal(get_db())
    recovery_owner = recovery_owner_instance_id or get_owner_instance_id()
    if not _OWNER_ID_RE.fullmatch(recovery_owner):
        raise OwnerIdentityError("recovery owner identifier is invalid")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size < 1:
        raise ValueError("page_size must be a positive integer")
    proof = owner_proof or (lambda owner: owner_process_proof(owner))
    if read_only_verifier is None:
        try:
            from app.agent.file_mutation.verifier import get_file_mutation_verifier

            read_only_verifier = get_file_mutation_verifier().verify
        except Exception as exc:
            # Native file writes without the verifier stay unresolved; startup
            # still hands off only after the existing process-death proof.
            logger.warning("file mutation recovery verifier unavailable error_type=%s", type(exc).__name__)
    proof_cache: dict[str, OwnerProof] = {}

    def confirm_owner_dead(owner: str) -> OwnerProof:
        if owner in proof_cache:
            return proof_cache[owner]
        state: OwnerProof = "unknown"
        for attempt in range(_OWNER_PROOF_RETRIES):
            try:
                state = proof(owner)
            except Exception:
                state = "unknown"
            if state != "live" or attempt == _OWNER_PROOF_RETRIES - 1:
                break
            # OS file-lock release may lag process wait/exit briefly on
            # Windows. A bounded recheck catches that cleanup window while
            # still leaving genuinely live owners untouched.
            time.sleep(_OWNER_PROOF_RETRY_SECONDS)
        proof_cache[owner] = state
        return state
    report = {
        "scanned": 0,
        "live_owner": 0,
        "owner_unverified": 0,
        "recovered": 0,
        "proof_based_handoff": 0,
        "paused": 0,
        "needs_review": 0,
        "verified_operations": 0,
        "conflicts": 0,
        "errors": 0,
        "snapshot_files_deleted": 0,
        "snapshot_bytes_deleted": 0,
    }
    after_run_id = ""
    while True:
        page = journal.list_recovery_runs(after_run_id=after_run_id, limit=page_size)
        if not page:
            break
        for candidate in page:
            report["scanned"] += 1
            run_id = str(candidate["run_id"])
            after_run_id = run_id
            old_owner = str(candidate.get("owner_instance_id") or "")
            if not old_owner or old_owner == recovery_owner:
                report["owner_unverified"] += 1
                continue
            lease_until = _parse_expiry(candidate.get("lease_expires_at"))
            if lease_until is None:
                report["owner_unverified"] += 1
                continue
            state = confirm_owner_dead(old_owner)
            if state == "live":
                report["live_owner"] += 1
                continue
            if state != "dead":
                report["owner_unverified"] += 1
                continue
            proof_based = lease_until > datetime.now(timezone.utc)
            if proof_based:
                report["proof_based_handoff"] += 1

            try:
                current = journal.load_run_internal(run_id)
                if current is None:
                    report["conflicts"] += 1
                    continue
                operations = _operation_rows(journal, run_id)
                handed_off = journal.release_expired_run(
                    run_id,
                    expected_owner_instance_id=old_owner,
                    recovery_owner_instance_id=recovery_owner,
                    expected_attempt=int(candidate["attempt"]),
                    expected_revision=int(candidate["revision"]),
                    safe_to_release=True,
                    owner_proven_dead=True,
                    reason_code="recovery_incomplete",
                )
                report["recovered"] += 1
                if read_only_verifier is not None:
                    report["verified_operations"] += _run_read_only_verifier(
                        journal,
                        run_id=run_id,
                        owner_instance_id=recovery_owner,
                        attempt=int(handed_off["attempt"]),
                        operations=operations,
                        verifier=read_only_verifier,
                    )
                unresolved_after_verification = _operation_rows(journal, run_id)
                still_uncertain_write = any(
                    operation["effect_class"] != "read_only"
                    and operation["status"] in _UNCERTAIN_WRITE_STATUSES
                    for operation in unresolved_after_verification
                )
                state_now = journal.load_run(run_id)
                if still_uncertain_write:
                    if state_now is not None and state_now["status"] == "interrupted":
                        state_now = journal.transition_run(
                            run_id,
                            to_status="needs_review",
                            owner_instance_id=recovery_owner,
                            attempt=int(state_now["attempt"]),
                            expected_revision=int(state_now["revision"]),
                            reason_code="external_effect_unknown",
                        )
                    if state_now is not None and state_now["status"] == "needs_review":
                        report["needs_review"] += 1
                elif state_now is not None and state_now["status"] in {"interrupted", "needs_review"}:
                    current_private = journal.load_run_internal(run_id)
                    if current_private is not None and _can_pause_recovered_run(journal, current_private):
                        journal.transition_run(
                            run_id,
                            to_status="paused",
                            owner_instance_id=recovery_owner,
                            attempt=int(state_now["attempt"]),
                            expected_revision=int(state_now["revision"]),
                            reason_code="recovery_checked",
                        )
                        report["paused"] += 1
                    elif state_now["status"] == "needs_review":
                        report["needs_review"] += 1
            except Exception as exc:
                report["errors"] += 1
                # Conflicts are expected if another process updated the row
                # after the page read; retain the latest state and fail closed.
                logger.warning(
                    "durable recovery handoff skipped run=%s error_type=%s",
                    run_id,
                    type(exc).__name__,
                )
        if len(page) < page_size:
            break
    try:
        report.update(_cleanup_file_mutation_snapshots(journal))
    except Exception as exc:
        logger.warning("file mutation snapshot cleanup failed error_type=%s", type(exc).__name__)
    return report


def _run_field(durable_run: object, name: str) -> Any:
    if isinstance(durable_run, Mapping):
        return durable_run.get(name)
    return getattr(durable_run, name, None)


def _workspace_key(value: object) -> str:
    raw = str(value or "").strip() or str(WORKSPACE_DIR)
    try:
        canonical = str(Path(raw).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        canonical = os.path.abspath(os.path.normpath(raw))
    return os.path.normcase(os.path.normpath(canonical))


def assert_run_dispatch_allowed(durable_run: object) -> None:
    """Enforce the owner fence and block overlapping active durable scopes."""
    journal = _run_field(durable_run, "journal")
    run_id = _run_field(durable_run, "run_id")
    owner_id = _run_field(durable_run, "owner_instance_id")
    attempt = _run_field(durable_run, "attempt")
    if (
        not isinstance(journal, Journal)
        or not isinstance(run_id, str)
        or isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt < 1
    ):
        raise DispatchConflictError("durable dispatch context is incomplete")
    current = journal.load_run_internal(run_id)
    if current is None or current.get("owner_instance_id") != owner_id or int(current.get("attempt") or 0) != attempt:
        raise DispatchConflictError("durable run owner or attempt is stale")
    if current.get("status") != "running":
        raise DispatchConflictError("durable run is not in a dispatchable state")
    expiry = _parse_expiry(current.get("lease_expires_at"))
    if expiry is None or expiry <= datetime.now(timezone.utc):
        raise DispatchConflictError("durable run lease has expired")
    if owner_id != get_owner_instance_id():
        raise DispatchConflictError("durable run is not owned by this process")

    conversation_id = str(current.get("conversation_id") or "")
    workspace_dir = _workspace_key(current.get("workspace_dir"))
    conversation_scope = journal.db.fetchone(
        "SELECT workspace_dir FROM conversations WHERE id = ?",
        (conversation_id,),
    )
    if conversation_scope is None or _workspace_key(conversation_scope["workspace_dir"]) != workspace_dir:
        raise DispatchConflictError("durable run workspace no longer matches its conversation")
    conflicting_statuses = _ACTIVE_RUN_STATUSES
    placeholders = ",".join("?" for _ in conflicting_statuses)
    rows = journal.db.fetchall(
        f"""SELECT run_id, conversation_id, workspace_dir, owner_instance_id, status FROM agent_runs
            WHERE run_id <> ? AND status IN ({placeholders})""",
        (run_id, *sorted(conflicting_statuses)),
    )
    for other in rows:
        same_conversation = bool(conversation_id) and str(other["conversation_id"] or "") == conversation_id
        other_workspace = _workspace_key(other["workspace_dir"])
        same_workspace = bool(workspace_dir) and other_workspace == workspace_dir
        quarantined = str(other["status"]) in {"interrupted", "needs_review"}
        other_owner = str(other["owner_instance_id"] or "")
        cross_process_scope = other_owner != owner_id
        unresolved_write = journal.db.fetchone(
            """SELECT 1 FROM run_operations WHERE run_id = ? AND effect_class <> 'read_only'
               AND status IN ('executing', 'uncertain', 'needs_review') LIMIT 1""",
            (str(other["run_id"]),),
        ) is not None
        # Healthy turns from this process may share the default/custom
        # workspace; per-operation resource locks arbitrate tool overlap.
        # A shared conversation is still single-owner. Quarantined runs,
        # unresolved writes, and cross-process workspace overlap stay closed.
        if same_conversation or (same_workspace and (quarantined or unresolved_write or cross_process_scope)):
            raise DispatchConflictError("another durable run owns this conversation or workspace")


# Compatibility name used by early lifecycle callers.
assert_dispatch_allowed = assert_run_dispatch_allowed
