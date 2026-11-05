"""Private Git trees for workspace changes. Never touches the user's index or HEAD.

Trees contain permitted, ordinary, non-ignored files; Git owns content storage and
diffs. Small manifests only bind run scope and filesystem identities for safe Undo.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import threading
import time
import uuid

from app.agent.runtime_paths import RUNTIME_DIR, WORKSPACE_DIR
from app.agent.file_mutation.fingerprint import fingerprint_path
from app.agent.file_mutation.planner import PathState

STORE = RUNTIME_DIR / "git-checkpoints"
LOCK = threading.RLock()
MAX_FILES = 2000
MAX_BYTES = 64 * 1024 * 1024


class ChangesError(RuntimeError):
    pass


def authorize(path: Path, *, mutate: bool = False, delete: bool = False) -> None:
    from app.agent.controller_policy import ActionType, resolve_permission
    for action in ([ActionType.READ, ActionType.DELETE if delete else ActionType.MUTATE] if mutate else [ActionType.READ]):
        decision = resolve_permission(action, target_path=str(path), allow_session_grants=False,
                                      consume_once_grants=False)
        if (not decision.allowed or decision.blocked or decision.requires_access_grant
                or (action == ActionType.READ and decision.requires_confirmation)):
            raise ChangesError("Current permissions do not allow access to this file.")


def _location(workspace: str, run_id: str) -> tuple[Path, Path, Path]:
    root = Path(os.path.abspath(workspace or WORKSPACE_DIR))
    # Reject linked workspace roots and parents using the same path boundary as Undo.
    fingerprint_path(root / ".monaw-checkpoint-boundary", max_bytes=0)
    key = hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()
    directory = STORE / key
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = directory / (hashlib.sha256(run_id.encode()).hexdigest() + ".json")
    return root, directory, record


def _git(root: Path, directory: Path, *args: str, data: bytes | None = None) -> bytes:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    command = ["git", "--literal-pathspecs", "--git-dir=" + str(directory / "objects.git"), "--work-tree=" + str(root),
               "-c", "core.hooksPath=" + os.devnull, "-c", "core.autocrlf=false",
               "-c", "core.quotePath=false", "-c", "core.longpaths=true", "-c", "core.fsmonitor=false", *args]
    if args[0] == "init":
        command = ["git", "-c", "init.templateDir=", "-c", "core.longpaths=true", *args]
    try:
        result = subprocess.run(command, input=data, capture_output=True, env=env, cwd=root, timeout=15,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ChangesError("Git is unavailable or the checkpoint exceeded its time limit.") from exc
    if result.returncode:
        raise ChangesError("Git could not read or save the workspace checkpoint.")
    return result.stdout


def _save(path: Path, record: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _target(root: Path, name: str) -> Path:
    parts = PurePosixPath(name).parts
    if not parts or any(p in {"..", ".git"} or ":" in p or "\\" in p for p in parts):
        raise ChangesError("Unsupported file path.")
    target = root.joinpath(*parts)
    if not target.is_relative_to(root) or PurePosixPath(name).is_absolute():
        raise ChangesError("The file is outside this workspace.")
    return target


def _capture(root: Path, directory: Path, previous: dict) -> dict:
    started = time.monotonic()
    _git(root, directory, "read-tree", "--empty")
    names = set(_git(root, directory, "ls-files", "--others", "--exclude-standard", "-z").decode("utf-8").split("\0"))
    names.discard("")
    names.update(previous.get("files", {}))
    if len(names) > MAX_FILES:
        raise ChangesError("Workspace exceeds the 2,000-file checkpoint limit.")
    files, absent, entries, excluded = {}, {}, [], []
    total = 0
    for name in sorted(names):
        if time.monotonic() - started > 20:
            raise ChangesError("Workspace checkpoint exceeded its time limit.")
        try:
            target = _target(root, name)
            # Runtime data must never be recursively checkpointed.
            if target.is_relative_to(RUNTIME_DIR.resolve()):
                continue
            authorize(target)
            value = fingerprint_path(target, include_bytes=True)
        except (OSError, ChangesError, ValueError):
            excluded.append(name)
            continue
        if not value.exists:
            absent[name] = asdict(PathState.from_fingerprint(value))
            continue
        total += value.size
        if total > MAX_BYTES:
            raise ChangesError("Workspace exceeds the 64 MB checkpoint limit.")
        old = previous.get("files", {}).get(name, {})
        oid = old.get("oid") if old.get("sha256") == value.sha256 else None
        if not oid:
            oid = _git(root, directory, "hash-object", "-w", "--stdin", data=value.data).decode().strip()
        state = asdict(PathState.from_fingerprint(value))
        files[name] = {**state, "oid": oid}
        entries.append(f"100644 {oid}\t{name}\0".encode("utf-8"))
    _git(root, directory, "update-index", "-z", "--index-info", data=b"".join(entries))
    tree = _git(root, directory, "write-tree").decode().strip()
    return {"tree": tree, "files": files, "absent": absent, "excluded": excluded}


def checkpoint(run_id: str, workspace: str, phase: str, *, overlapping: bool = False) -> None:
    """Best effort: checkpoint failure never prevents the agent from answering."""
    with LOCK:
        record = {"run_id": run_id}
        try:
            root, directory, path = _location(workspace, run_id)
            record = json.loads(path.read_text()) if path.exists() else {"run_id": run_id, "workspace": str(root)}
            if phase == "before" and "before" in record:
                return
            if phase == "after" and "before" not in record:
                return
            if not (directory / "objects.git").exists():
                _git(root, directory, "init", "--bare", str(directory / "objects.git"))
            record[phase] = _capture(root, directory, record.get("before", {}))
            _git(root, directory, "update-ref", "refs/monaw/" + path.stem + "/" + phase, record[phase]["tree"])
            record["overlapping"] = record.get("overlapping", False) or overlapping
            record.pop("error", None)
            _save(path, record)
        except Exception as exc:
            if "path" in locals():
                record["error"] = str(exc) if isinstance(exc, ChangesError) else "Workspace checkpoint could not be saved."
                try:
                    _save(path, record)
                except OSError:
                    pass


def reclaim_checkpoints(db, *, clear_terminal: bool = False) -> dict:
    """Remove orphaned/expired refs, then let Git reclaim unreachable content.

    Retention retries failures, including a crash after conversation deletion.
    Live and recoverable runs retain their original baseline.
    """
    from datetime import datetime, timedelta, timezone
    removed = 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with LOCK:
        if not STORE.exists() or STORE.is_symlink():
            return {"git_checkpoints_deleted": 0}
        for directory in STORE.iterdir():
            if not directory.is_dir() or directory.is_symlink() or len(directory.name) != 64:
                continue
            dirty = False
            for path in directory.glob("*.json"):
                try:
                    if path.is_symlink():
                        continue
                    record = json.loads(path.read_text())
                    run_id = record.get("run_id", "")
                    if path.stem != hashlib.sha256(run_id.encode()).hexdigest():
                        continue
                    run = db.fetchone("SELECT status, finished_at FROM agent_runs WHERE run_id = ?", (run_id,))
                    if run is not None and (run["status"] not in {"completed", "failed", "cancelled"}
                            or (not clear_terminal and str(run["finished_at"] or "9999") >= cutoff)):
                        continue
                    for phase in ("before", "after"):
                        _git(directory, directory, "update-ref", "-d", "refs/monaw/" + path.stem + "/" + phase)
                    path.unlink()
                    removed += 1
                    dirty = True
                except (OSError, ValueError, ChangesError):
                    continue
            # Also retry pruning after a previous partial cleanup/crash.
            if dirty or (directory / "objects.git").exists():
                try:
                    _git(directory, directory, "read-tree", "--empty")
                    _git(directory, directory, "prune", "--expire", "now")
                except ChangesError:
                    pass
    return {"git_checkpoints_deleted": removed}


def _load(workspace: str, run_id: str) -> tuple[Path, Path, Path, dict]:
    root, directory, path = _location(workspace, run_id)
    if not path.exists():
        raise ChangesError("This older run has no Git checkpoint. New runs will track workspace changes.")
    record = json.loads(path.read_text())
    if record.get("workspace") != str(root) or record.get("run_id") != run_id:
        raise ChangesError("Checkpoint scope does not match this run.")
    if record.get("error"):
        raise ChangesError(record["error"])
    if "after" not in record:
        raise ChangesError("Changes will be available when this run finishes.")
    return root, directory, path, record


def _matches(value, state: dict | None) -> bool:
    if state is None:
        return not value.exists
    if not state["exists"]:
        return not value.exists and list(value.parent_identity or ()) == list(state["parent_identity"] or ())
    return (value.exists and value.sha256 == state["sha256"] and
            list(value.identity or ()) == list(state["identity"] or ()) and
            list(value.parent_identity or ()) == list(state["parent_identity"] or ()))


def changes(workspace: str, run_id: str, *, name: str | None = None, inverse: bool = False) -> dict:
    with LOCK:
        root, directory, _, record = _load(workspace, run_id)
        before, after = record["before"], record["after"]
        excluded = set(before["excluded"]) | set(after["excluded"])
        output = _git(root, directory, "diff-tree", "--no-commit-id", "-r", "--no-renames", "--numstat", "-z", before["tree"], after["tree"])
        files = []
        for row in output.decode("utf-8").split("\0"):
            if not row:
                continue
            added, removed, filename = row.split("\t", 2)
            if filename in excluded or (name is not None and filename != name):
                continue
            target = _target(root, filename)
            try:
                authorize(target)
            except ChangesError:
                excluded.add(filename)
                continue
            undo = "available"
            if filename in record.get("undone", []):
                undo = "undone"
            elif filename in record.get("pending", []):
                undo = "needs_review"
            elif record.get("overlapping"):
                undo = "overlapping_runs"
            else:
                try:
                    authorize(target, mutate=True, delete=filename not in before["files"])
                    if not _matches(fingerprint_path(target), after["files"].get(filename) or after.get("absent", {}).get(filename)):
                        undo = "conflict"
                    elif filename not in before["files"] and os.name != "nt":
                        undo = "unsupported"
                except (OSError, ChangesError):
                    undo = "unavailable"
            item = {"path": filename, "added": int(added) if added != "-" else None,
                    "removed": int(removed) if removed != "-" else None, "undo": undo,
                    "action": "added" if filename not in before["files"] else "deleted" if filename not in after["files"] else "edited"}
            if name is not None:
                trees = (after["tree"], before["tree"]) if inverse else (before["tree"], after["tree"])
                patch = _git(root, directory, "diff-tree", "-p", "--no-ext-diff", "--no-textconv", "--no-renames", *trees, "--", filename)
                item["diff"] = patch[:200_000].decode("utf-8", errors="replace")
                item["truncated"] = len(patch) > 200_000
            files.append(item)
        return {"files": files, "excluded_count": len(excluded), "source": "git", "run_id": run_id}


def undo(workspace: str, run_id: str, name: str) -> None:
    """Restore one exact checkpoint entry. Preserve staged files and later edits."""
    from app.agent.file_mutation.undo_manager import UndoManager, _atomic_rename_no_replace
    with LOCK:
        root, directory, path, record = _load(workspace, run_id)
        entry = changes(workspace, run_id, name=name)["files"]
        if not entry or entry[0]["undo"] != "available":
            raise ChangesError("Undo is unavailable: the file changed or needs review.")
        target = _target(root, name)
        authorize(target, mutate=True, delete=name not in record["before"]["files"])
        current = fingerprint_path(target)
        if not _matches(current, record["after"]["files"].get(name) or record["after"].get("absent", {}).get(name)):
            raise ChangesError("The file changed. It was left untouched.")
        before = record["before"]["files"].get(name)
        payload = _git(root, directory, "cat-file", "blob", before["oid"]) if before else None
        # Persist the claim before the effect. A crash must never invite blind replay.
        record.setdefault("pending", []).append(name)
        _save(path, record)
        expected = PathState.from_fingerprint(current)
        if payload is None:
            UndoManager._remove_exact(target, expected, uuid.uuid4().hex, confirmation_granted=True)
        elif current.exists:
            UndoManager._write_existing_exact(target, expected, payload, None)
        else:
            stage, _ = UndoManager._stage_new_file(uuid.uuid4().hex, target, payload, target_modified_ns=None)
            _atomic_rename_no_replace(stage, target, expected_source_parent=current.parent_identity,
                                      expected_destination_parent=current.parent_identity)
        result = fingerprint_path(target)
        if (payload is None and result.exists) or (payload is not None and result.sha256 != before["sha256"]):
            raise ChangesError("Undo could not be verified. Review the file before continuing.")
        record["pending"].remove(name)
        record.setdefault("undone", []).append(name)
        _save(path, record)
