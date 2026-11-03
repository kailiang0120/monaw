"""Read-only verifier for snapshot-bound native file operation recovery."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from app.agent.file_mutation.fingerprint import FingerprintError, canonical_path, fingerprint_path
from app.agent.file_mutation.snapshot_store import (
    SnapshotStore,
    SnapshotStoreError,
    _safe_root_identity,
    get_snapshot_store,
)

def _current_read_policy(path: str) -> object:
    from app.agent.controller_policy import resolve_permission
    from app.agent.permission_actions import ActionType

    # Recovery has no live approval UI and must not consume or reuse a once or
    # session grant that belonged to the interrupted turn.
    return resolve_permission(ActionType.READ, target_path=path, allow_session_grants=False)


def _unknown() -> dict[str, object]:
    return {"verification_result": "unknown"}


def _state_matches(current: object, expected: Mapping[str, object]) -> bool:
    if not hasattr(current, "exists"):
        return False
    if bool(getattr(current, "exists")) != bool(expected.get("exists")):
        return False
    parent = expected.get("parent_identity")
    if list(getattr(current, "parent_identity", ())) != parent:
        return False
    if not expected.get("exists"):
        return getattr(current, "kind", None) == "absent"
    content_identity_matches = bool(
        getattr(current, "kind", None) == "file"
        and getattr(current, "size", None) == expected.get("size")
        and getattr(current, "sha256", None) == expected.get("sha256")
        and list(getattr(current, "identity", ()) or ()) == expected.get("identity")
    )
    if not content_identity_matches:
        return False
    expected_mtime = expected.get("modified_ns")
    if expected_mtime is not None and getattr(current, "modified_ns", None) != expected_mtime:
        return False
    expected_created = expected.get("created_ns")
    if os.name == "nt" and expected_created is not None and getattr(current, "created_ns", None) != expected_created:
        return False
    return True


def _scope_is_readable(operand: Mapping[str, Any]) -> bool:
    path = Path(str(operand["path"]))
    root = Path(str(operand["authorized_root"]))
    if operand.get("authorized_root_kind") == "directory":
        try:
            if list(_safe_root_identity(root)) != operand.get("authorized_root_identity"):
                return False
        except (OSError, SnapshotStoreError, TypeError, ValueError):
            return False
        return os.access(root, os.R_OK | os.X_OK)
    if root != path:
        return False
    # For an exact-file READ authorization, check only its parent scope when
    # the operation has removed the file. The full parent chain is checked by
    # fingerprint_path before any content read.
    return os.access(path.parent, os.R_OK | os.X_OK)


def _fingerprint_operand(operand: Mapping[str, Any]):
    path_value = operand.get("path")
    if not isinstance(path_value, str) or str(canonical_path(path_value)) != path_value:
        raise FingerprintError("manifest path is not canonical")
    root_value = operand.get("authorized_root")
    if not isinstance(root_value, str) or str(canonical_path(root_value)) != root_value:
        raise FingerprintError("manifest scope is not canonical")
    path = Path(path_value)
    root = Path(root_value)
    try:
        if path != root and not path.is_relative_to(root):
            raise FingerprintError("operand is outside its saved scope")
    except (OSError, RuntimeError, ValueError) as exc:
        raise FingerprintError("operand scope is invalid") from exc
    if not _scope_is_readable(operand):
        raise FingerprintError("saved read scope is no longer available")
    return fingerprint_path(path, max_bytes=8 * 1024 * 1024)


def _operand_by_role(operands: list[dict[str, Any]], *roles: str) -> dict[str, Any] | None:
    wanted = {role.lower() for role in roles}
    matches = [item for item in operands if str(item.get("role") or "").lower() in wanted]
    return matches[0] if len(matches) == 1 else None


class FileMutationVerifier:
    """Verify only exact authenticated operands under their saved READ scope."""

    def __init__(
        self,
        store: SnapshotStore | None = None,
        *,
        read_policy: Callable[[str], object] | None = None,
    ) -> None:
        self.store = store or get_snapshot_store()
        self.read_policy = read_policy or _current_read_policy

    def _read_is_allowed(self, path: str) -> bool:
        try:
            decision = self.read_policy(path)
        except Exception:
            return False
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

    def verify(self, private_operation: Mapping[str, Any]) -> dict[str, object]:
        """Return typed, path-free evidence; every uncertainty stays unknown."""
        if not isinstance(private_operation, Mapping):
            return _unknown()
        run_id = private_operation.get("run_id")
        operation_id = private_operation.get("operation_id")
        operation_kind = private_operation.get("operation_kind")
        reference = private_operation.get("snapshot_ref")
        if (
            not isinstance(run_id, str)
            or not isinstance(operation_id, str)
            or operation_kind not in {"file_write", "file_move", "file_copy", "file_delete"}
            or private_operation.get("effect_class") != "verifiable_write"
            or private_operation.get("status") not in {"executing", "uncertain", "needs_review"}
            or not isinstance(reference, str)
        ):
            return _unknown()
        try:
            snapshot = self.store.load(reference, run_id=run_id, operation_id=operation_id)
            if snapshot.get("operation_kind") != operation_kind:
                return _unknown()
            operands = snapshot.get("operands")
            if not isinstance(operands, list) or not operands:
                return _unknown()
            current: dict[str, object] = {}
            for operand in operands:
                if not isinstance(operand, dict):
                    return _unknown()
                path_value = operand.get("path")
                if not isinstance(path_value, str) or not self._read_is_allowed(path_value):
                    return _unknown()
                fingerprint = _fingerprint_operand(operand)
                current[str(operand["path"])] = fingerprint
            return self._verify_loaded(operation_kind, operands, current)
        except (OSError, RuntimeError, SnapshotStoreError, FingerprintError, TypeError, ValueError, KeyError):
            return _unknown()

    def _verify_loaded(
        self,
        operation_kind: str,
        operands: list[dict[str, Any]],
        current: Mapping[str, object],
    ) -> dict[str, object]:
        if operation_kind == "file_write":
            return self._verify_write(operands, current)
        if operation_kind == "file_move":
            return self._verify_move(operands, current)
        if operation_kind == "file_copy":
            return self._verify_copy(operands, current)
        if operation_kind == "file_delete":
            return self._verify_delete(operands, current)
        return _unknown()

    @staticmethod
    def _verify_write(operands: list[dict[str, Any]], current: Mapping[str, object]) -> dict[str, object]:
        if len(operands) != 1:
            return _unknown()
        operand = operands[0]
        before = operand.get("before")
        after = operand.get("after")
        strategy = operand.get("identity_strategy")
        observed = current.get(str(operand.get("path")))
        if not isinstance(before, dict) or not isinstance(after, dict) or observed is None:
            return _unknown()
        unchanged = _state_matches(observed, before)
        completed = False
        if after.get("exists") and getattr(observed, "exists", False):
            identity = list(getattr(observed, "identity", ()) or ())
            expected_identity = after.get("identity")
            identity_ok = expected_identity is None or identity == expected_identity
            if strategy == "same":
                identity_ok = identity_ok and identity == before.get("identity")
            elif strategy == "replaced" and before.get("exists"):
                identity_ok = identity_ok and identity != before.get("identity")
            elif strategy not in {"same", "replaced"}:
                identity_ok = False
            completed = bool(
                identity_ok
                and list(getattr(observed, "parent_identity", ())) == after.get("parent_identity")
                and getattr(observed, "kind", None) == "file"
                and getattr(observed, "sha256", None) == after.get("sha256")
                and getattr(observed, "size", None) == after.get("size")
            )
        elif not after.get("exists"):
            completed = bool(
                not getattr(observed, "exists", True)
                and list(getattr(observed, "parent_identity", ())) == after.get("parent_identity")
                and strategy == "absent"
            )
        if completed and unchanged:
            return _unknown()
        if completed:
            return {
                "verification_result": "completed",
                "target_exists": bool(getattr(observed, "exists", False)),
                "identity_matches": True,
                "sha256_matches": True,
            }
        if unchanged:
            return {
                "verification_result": "unchanged",
                "target_exists": bool(getattr(observed, "exists", False)),
                "identity_matches": True,
                "sha256_matches": True,
            }
        return _unknown()

    @staticmethod
    def _verify_move(operands: list[dict[str, Any]], current: Mapping[str, object]) -> dict[str, object]:
        sources = [item for item in operands if item.get("before", {}).get("exists") and not item.get("after", {}).get("exists")]
        destinations = [item for item in operands if str(item.get("role") or "").lower() in {"destination", "dest", "target"}]
        if len(sources) != 1 or len(destinations) != 1:
            return _unknown()
        source, destination = sources[0], destinations[0]
        source_before = source["before"]
        destination_before = destination["before"]
        destination_after = destination["after"]
        source_now = current.get(str(source["path"]))
        destination_now = current.get(str(destination["path"]))
        if source_now is None or destination_now is None:
            return _unknown()
        source_unchanged = _state_matches(source_now, source_before)
        destination_unchanged = _state_matches(destination_now, destination_before)
        unchanged = source_unchanged and destination_unchanged
        destination_identity = list(getattr(destination_now, "identity", ()) or ())
        completed = bool(
            _state_matches(source_now, source["after"])
            and _state_matches(destination_now, destination_after)
            and destination_identity == source_before.get("identity")
            and getattr(destination_now, "sha256", None) == source_before.get("sha256")
        )
        if completed and unchanged:
            return _unknown()
        evidence: dict[str, object] = {
            "source_exists": bool(getattr(source_now, "exists", False)),
            "destination_exists": bool(getattr(destination_now, "exists", False)),
            "source_identity_matches": source_unchanged,
            "destination_identity_matches": completed or destination_unchanged,
            "source_sha256_matches": source_unchanged,
            "destination_sha256_matches": completed or destination_unchanged,
        }
        if completed:
            evidence["verification_result"] = "completed"
            return evidence
        if unchanged:
            evidence["verification_result"] = "unchanged"
            return evidence
        return _unknown()

    @staticmethod
    def _verify_copy(operands: list[dict[str, Any]], current: Mapping[str, object]) -> dict[str, object]:
        sources = [item for item in operands if str(item.get("role") or "").lower() == "source"]
        destinations = [item for item in operands if str(item.get("role") or "").lower() in {"destination", "dest", "target"}]
        if len(sources) != 1 or len(destinations) != 1:
            return _unknown()
        source, destination = sources[0], destinations[0]
        source_before = source["before"]
        destination_before, destination_after = destination["before"], destination["after"]
        source_now = current.get(str(source["path"]))
        destination_now = current.get(str(destination["path"]))
        if source_now is None or destination_now is None:
            return _unknown()
        source_unchanged = _state_matches(source_now, source_before)
        destination_unchanged = _state_matches(destination_now, destination_before)
        unchanged = source_unchanged and destination_unchanged
        destination_identity = list(getattr(destination_now, "identity", ()) or ())
        identity_ok = bool(
            getattr(destination_now, "exists", False)
            and destination_identity
            and destination_identity != source_before.get("identity")
            and (destination_before.get("identity") is None or destination_identity != destination_before.get("identity"))
            and destination_identity == destination_after.get("identity")
        )
        completed = bool(
            source_unchanged
            and identity_ok
            and list(getattr(destination_now, "parent_identity", ())) == destination_after.get("parent_identity")
            and getattr(destination_now, "sha256", None) == source_before.get("sha256")
            and getattr(destination_now, "size", None) == source_before.get("size")
        )
        if completed and unchanged:
            return _unknown()
        evidence: dict[str, object] = {
            "source_exists": bool(getattr(source_now, "exists", False)),
            "destination_exists": bool(getattr(destination_now, "exists", False)),
            "source_identity_matches": source_unchanged,
            "destination_identity_matches": completed or destination_unchanged,
            "source_sha256_matches": source_unchanged,
            "destination_sha256_matches": completed or destination_unchanged,
        }
        if completed:
            evidence["verification_result"] = "completed"
            return evidence
        if unchanged:
            evidence["verification_result"] = "unchanged"
            return evidence
        return _unknown()

    @staticmethod
    def _verify_delete(operands: list[dict[str, Any]], current: Mapping[str, object]) -> dict[str, object]:
        if len(operands) != 1:
            return _unknown()
        operand = operands[0]
        before, after = operand.get("before"), operand.get("after")
        observed = current.get(str(operand.get("path")))
        if not isinstance(before, dict) or not isinstance(after, dict) or observed is None:
            return _unknown()
        unchanged = _state_matches(observed, before)
        completed = bool(
            not getattr(observed, "exists", True)
            and not after.get("exists")
            and list(getattr(observed, "parent_identity", ())) == after.get("parent_identity")
        )
        if completed and unchanged:
            return _unknown()
        if completed:
            return {
                "verification_result": "completed",
                "target_exists": False,
                "identity_matches": False,
                "sha256_matches": False,
            }
        if unchanged:
            return {
                "verification_result": "unchanged",
                "target_exists": True,
                "identity_matches": True,
                "sha256_matches": True,
            }
        return _unknown()


_DEFAULT_VERIFIER: FileMutationVerifier | None = None


def get_file_mutation_verifier() -> FileMutationVerifier:
    global _DEFAULT_VERIFIER
    if _DEFAULT_VERIFIER is None:
        _DEFAULT_VERIFIER = FileMutationVerifier()
    return _DEFAULT_VERIFIER
