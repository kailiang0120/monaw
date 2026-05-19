import json
import time
from pathlib import Path

import pytest

from app.agent.sandbox.path_policy import (
    SandboxPathPolicy,
    cleanup_old_runs,
    collect_copy_out,
    copy_in_file,
    create_run_workspace,
    ensure_allowed_path,
    write_artifact_manifest,
)


def test_copy_in_allowed_file(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "input.txt"
    source.write_text("hello", encoding="utf-8")
    workspace = create_run_workspace("test-copy-in")
    policy = SandboxPathPolicy.from_strings(allowed_input_roots=[str(source_root)])

    copied = copy_in_file(source, workspace, policy)

    assert copied.read_text(encoding="utf-8") == "hello"


def test_copy_in_blocked_root_denied(tmp_path):
    allowed = tmp_path / "allowed"
    blocked = tmp_path / "blocked"
    allowed.mkdir()
    blocked.mkdir()
    source = blocked / "input.txt"
    source.write_text("no", encoding="utf-8")
    policy = SandboxPathPolicy.from_strings(allowed_input_roots=[str(allowed)])

    with pytest.raises(PermissionError):
        copy_in_file(source, create_run_workspace("test-blocked-root"), policy)


def test_copy_out_manifest_contains_expected_files(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "result.txt").write_text("ok", encoding="utf-8")
    destination = tmp_path / "destination"
    destination.mkdir()
    policy = SandboxPathPolicy.from_strings(
        allowed_output_roots=[str(destination)],
        max_copy_out_bytes=1024,
    )

    copied = collect_copy_out(output, destination, policy)
    manifest_path = write_artifact_manifest("test-manifest", {"copy_out": copied})

    try:
        payload = json.loads(open(manifest_path, encoding="utf-8").read())
        assert payload["artifacts"]["copy_out"][0]["destination"].endswith("result.txt")
    finally:
        Path(manifest_path).unlink(missing_ok=True)


def test_copy_out_size_limit_enforced(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "large.txt").write_text("x" * 20, encoding="utf-8")
    destination = tmp_path / "destination"
    destination.mkdir()
    policy = SandboxPathPolicy.from_strings(
        allowed_output_roots=[str(destination)],
        max_copy_out_bytes=10,
    )

    with pytest.raises(PermissionError):
        collect_copy_out(output, destination, policy)


def test_path_canonicalization_blocks_prefix_tricks(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    blocked = tmp_path / "allowed-but-not-really"
    blocked.mkdir()
    target = blocked / "file.txt"
    target.write_text("no", encoding="utf-8")

    with pytest.raises(PermissionError):
        ensure_allowed_path(target, [allowed], purpose="input")


def test_create_run_workspace_rejects_path_traversal_ids():
    with pytest.raises(ValueError):
        create_run_workspace(r"..\escape-check")


def test_artifact_cleanup_removes_old_runs():
    workspace = create_run_workspace("test-old-run")
    old_time = time.time() - 3600
    workspace.touch()
    import os

    os.utime(workspace, (old_time, old_time))

    removed = cleanup_old_runs(older_than_seconds=1)

    assert str(workspace) in removed
