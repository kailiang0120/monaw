"""Main Git checkpoint workflow: dirty worktrees, command edits, and safe Undo."""
import os
from pathlib import Path
import subprocess

import pytest

from app.agent import git_changes as tracking


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(tracking, "STORE", tmp_path / "private")
    monkeypatch.setattr(tracking, "authorize", lambda *a, **kw: None)
    return root


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])


def test_git_tracks_patch_and_command_edits_preserving_dirty_index(workspace):
    root = workspace
    git(root, "init")
    first, second = root / "one.txt", root / "two.txt"
    first.write_bytes(b"original\n")
    second.write_bytes(b"original\n")
    git(root, "add", ".")
    index_before = git(root, "ls-files", "--stage")
    first.write_bytes(b"user's unstaged edit\r\n")
    (root / ".gitignore").write_text("ignored.txt\n")
    (root / "ignored.txt").write_text("not tracked")
    tracking.checkpoint("run", str(root), "before")
    first.write_bytes(b"agent edit\n")
    second.write_bytes(b"command edit\nextra\n")
    tracking.checkpoint("run", str(root), "after")
    result = tracking.changes(str(root), "run")
    assert {item["path"] for item in result["files"]} == {"one.txt", "two.txt"}
    assert result["files"][1]["added"] == 2
    inverse = tracking.changes(str(root), "run", name="one.txt", inverse=True)["files"][0]["diff"]
    assert "+user's unstaged edit" in inverse and "-agent edit" in inverse
    tracking.undo(str(root), "run", "one.txt")
    assert first.read_bytes() == b"user's unstaged edit\r\n"
    assert second.read_bytes() == b"command edit\nextra\n"
    assert git(root, "ls-files", "--stage") == index_before
    assert tracking.changes(str(root), "run")["files"][0]["undo"] == "undone"
    with pytest.raises(tracking.ChangesError):
        tracking.undo(str(root), "run", "one.txt")


def test_later_edits_permissions_and_traversal_block_undo(workspace, monkeypatch):
    target = workspace / "file.txt"
    target.write_text("before")
    tracking.checkpoint("run", str(workspace), "before")
    target.write_text("after")
    tracking.checkpoint("run", str(workspace), "after")
    target.write_text("external edit")
    with pytest.raises(tracking.ChangesError):
        tracking.undo(str(workspace), "run", "file.txt")
    assert target.read_text() == "external edit"
    with pytest.raises(tracking.ChangesError):
        tracking.undo(str(workspace), "run", "../file.txt")
    monkeypatch.setattr(tracking, "authorize", lambda *a, **kw: (_ for _ in ()).throw(tracking.ChangesError("denied")))
    assert tracking.changes(str(workspace), "run")["files"] == []


def test_deleted_files_restore_and_new_files_are_tracked(workspace):
    deleted = workspace / "deleted.txt"
    deleted.write_bytes(b"restore me\n")
    tracking.checkpoint("run", str(workspace), "before")
    deleted.unlink()
    (workspace / "created.txt").write_bytes(b"new\n")
    tracking.checkpoint("run", str(workspace), "after")
    assert len(tracking.changes(str(workspace), "run")["files"]) == 2
    tracking.undo(str(workspace), "run", "deleted.txt")
    assert deleted.read_bytes() == b"restore me\n"
    # Windows uses the existing identity-bound delete primitive.
    if os.name != "nt":
        assert tracking.changes(str(workspace), "run")["files"][0]["undo"] == "unsupported"


def test_old_runs_and_overlapping_runs_do_not_offer_unsafe_undo(workspace):
    with pytest.raises(tracking.ChangesError, match="no Git checkpoint"):
        tracking.changes(str(workspace), "old")
    target = workspace / "file.txt"
    target.write_text("before")
    tracking.checkpoint("run", str(workspace), "before", overlapping=True)
    target.write_text("after")
    tracking.checkpoint("run", str(workspace), "after")
    assert tracking.changes(str(workspace), "run")["files"][0]["undo"] == "overlapping_runs"


def test_git_never_runs_workspace_filters(workspace):
    (workspace / ".gitattributes").write_text("*.txt filter=evil diff=evil\n")
    (workspace / "test.txt").write_text("hello")
    tracking.checkpoint("run", str(workspace), "before")
    (workspace / "test.txt").write_text("world")
    tracking.checkpoint("run", str(workspace), "after")
    assert "+world" in tracking.changes(str(workspace), "run", name="test.txt")["files"][0]["diff"]


def test_deleted_chat_reclaims_git_content_but_live_run_is_retained(workspace):
    from types import SimpleNamespace
    (workspace / "file.txt").write_text("private content")
    tracking.checkpoint("run", str(workspace), "before")
    _, directory, record_path = tracking._location(str(workspace), "run")
    import json
    oid = json.loads(record_path.read_text())["before"]["files"]["file.txt"]["oid"]
    live = SimpleNamespace(fetchone=lambda *args: {"status": "paused", "finished_at": None})
    assert tracking.reclaim_checkpoints(live, clear_terminal=True)["git_checkpoints_deleted"] == 0
    deleted = SimpleNamespace(fetchone=lambda *args: None)
    assert tracking.reclaim_checkpoints(deleted)["git_checkpoints_deleted"] == 1
    assert not record_path.exists()
    with pytest.raises(tracking.ChangesError):
        tracking._git(workspace, directory, "cat-file", "blob", oid)
