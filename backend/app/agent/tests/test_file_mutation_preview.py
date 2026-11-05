from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.file_mutation.diff_engine import unified_diff_preview
from app.agent.file_mutation.planner import PathState, PlanOperand, build_plan
from app.agent.file_mutation.snapshot_store import SnapshotStore
from app.agent.settings_store import AgentSettings, PathRule, save_agent_settings
from app.skills.filesystem import file_ops


@pytest.fixture
def mutation_policy_env(tmp_path, monkeypatch):
    import app.agent.controller_policy as controller_policy

    policy_dir = tmp_path / "policy"
    policy_dir.mkdir()
    monkeypatch.setattr(controller_policy, "_POLICY_DIR", policy_dir)
    monkeypatch.setattr(controller_policy, "_POLICY_FILE", policy_dir / "controller_policy.md")
    monkeypatch.setattr(controller_policy, "_ALLOWLIST_FILE", policy_dir / "allowlisted_apps.md")
    root = tmp_path / "workspace"
    root.mkdir()

    def grant(*, read: bool, write: bool) -> None:
        settings = AgentSettings()
        settings.permissions.mode = "custom"
        settings.permissions.confirmations.mutate = False
        settings.permissions.path_rules = [
            PathRule(
                path=str(root),
                read=read,
                write=write,
                delete=True,
                launch=False,
                enabled=True,
            )
        ]
        save_agent_settings(settings, settings_path=policy_dir.parent / "settings.json")

    return SimpleNamespace(root=root, grant=grant)


def _reject_snapshot_capture(*_args, **_kwargs):
    pytest.fail("dry-run must not create a snapshot")


def _operand(path: str, before: bytes, after: bytes, *, identity=(3, 8)) -> PlanOperand:
    before_state = PathState.from_bytes(path, before, identity=identity, parent_identity=(3, 2))
    after_state = PathState.from_bytes(path, after, identity=identity, parent_identity=(3, 2))
    return PlanOperand(path, "target", before_state, after_state, identity_strategy="same")


def test_metadata_preview_does_not_expose_supplied_contents_or_private_fingerprints():
    path = r"C:\workspace\secret.txt"
    secret = b"confidential old content"
    operand = _operand(path, secret, b"new content")

    plan = build_plan(
        "write",
        {"path": path, "content": "new content"},
        [operand],
        before_bytes=secret,
        after_bytes=b"new content",
        read_authorized=False,
    )
    public = plan.to_preview_dict()
    serialized = repr(public)

    assert public["preview"] == {"kind": "summary", "reason": "read_permission_required"}
    assert secret.decode() not in serialized
    assert "new content" not in serialized
    assert "sha256" not in serialized
    assert "identity" not in serialized
    assert plan.private_manifest()["operands"][0]["before"]["sha256"]


def test_plan_is_immutable_and_binding_tracks_content_and_identity():
    path = r"C:\workspace\notes.txt"
    first_operand = _operand(path, b"before", b"after", identity=(3, 8))
    second_operand = _operand(path, b"before", b"after", identity=(3, 9))
    first = build_plan("write", {"path": path, "content": "after"}, [first_operand])
    same = build_plan("write", {"path": path, "content": "after"}, [first_operand])
    changed_content = build_plan("write", {"path": path, "content": "different"}, [first_operand])
    changed_identity = build_plan("write", {"path": path, "content": "after"}, [second_operand])

    assert first.same_binding(same)
    assert not first.same_binding(changed_content)
    assert not first.same_binding(changed_identity)
    with pytest.raises(FrozenInstanceError):
        first.operation = "delete"


def test_planner_is_pure_and_does_not_open_target_paths(monkeypatch):
    def fail_read(*_args, **_kwargs):
        raise AssertionError("planner attempted to read a target")

    monkeypatch.setattr("pathlib.Path.read_bytes", fail_read)
    path = r"C:\does-not-exist\target.txt"
    before = PathState.metadata(path, exists=False, kind="absent", size=0, parent_identity=(5, 7))
    after = PathState.from_bytes(path, b"generated")
    operand = PlanOperand(path, "target", before, after, identity_strategy="replaced")

    plan = build_plan(
        "write",
        {"path": path, "content": "generated"},
        [operand],
        after_bytes=b"generated",
        read_authorized=True,
    )

    assert plan.to_preview_dict()["preview"]["kind"] == "unified_diff"


def test_unified_diff_allows_utf16_and_supported_legacy_text_encoding():
    utf16 = unified_diff_preview("old\n".encode("utf-16"), "new\n".encode("utf-16"))
    cp1252 = unified_diff_preview("café\n".encode("cp1252"), "côté\n".encode("cp1252"), encoding="cp1252")

    assert utf16.kind == "unified_diff"
    assert "-old" in utf16.text and "+new" in utf16.text
    assert cp1252.kind == "unified_diff"
    assert "café" in cp1252.text and "côté" in cp1252.text


def test_unified_diff_summarizes_binary_or_undecodable_bytes_without_leaking_content():
    private_bytes = b"PRIVATE\x00payload"
    preview = unified_diff_preview(private_bytes, b"other\x00payload")

    assert preview.kind == "summary"
    assert preview.reason == "binary_or_undecodable"
    assert "PRIVATE" not in repr(preview.to_dict())


def test_unified_diff_preserves_missing_final_newline_and_escapes_format_controls():
    newline_change = unified_diff_preview(b"same", b"same\n")
    bidi_change = unified_diff_preview(b"safe\n", "safe\u202eevil\n".encode("utf-8"))

    assert "\\ No newline at end of file" in newline_change.text
    assert "\\u202e" in bidi_change.text
    assert "\u202e" not in bidi_change.text


def test_unified_diff_enforces_input_line_and_output_limits():
    input_limited = unified_diff_preview(b"1234", b"5678", max_input_bytes=4)
    line_limited = unified_diff_preview(b"a\nb\n", b"x\ny\n", max_input_lines=1)
    output_limited = unified_diff_preview(
        b"old value\n",
        b"new value\n",
        max_output_bytes=40,
        max_output_lines=2,
    )
    newline_marker_limited = unified_diff_preview(b"old", b"new", max_output_lines=5)

    assert input_limited.kind == "summary" and input_limited.reason == "input_limit"
    assert line_limited.kind == "summary" and line_limited.reason == "line_limit"
    assert output_limited.truncated is True
    assert len(output_limited.text.encode("utf-8")) <= 40
    assert newline_marker_limited.truncated is True
    assert len(newline_marker_limited.text.splitlines()) <= 5


def test_native_write_dry_run_previews_authorized_diff_without_writing_or_snapshot(
    mutation_policy_env,
    monkeypatch,
):
    mutation_policy_env.grant(read=True, write=True)
    target = mutation_policy_env.root / "notes.txt"
    original = b"private original line\n"
    target.write_bytes(original)
    monkeypatch.setattr(SnapshotStore, "capture", _reject_snapshot_capture)

    result = json.loads(
        file_ops.file_write(
            str(target),
            "updated line\n",
            overwrite=True,
            dry_run=True,
        )
    )

    assert result["status"] == "ok"
    assert result["dry_run"] is True
    preview = result["preview"]["preview"]
    assert preview["kind"] == "unified_diff"
    assert "-private original line" in preview["text"]
    assert "+updated line" in preview["text"]
    assert target.read_bytes() == original


def test_native_write_dry_run_is_metadata_only_without_read_permission(
    mutation_policy_env,
    monkeypatch,
):
    mutation_policy_env.grant(read=False, write=True)
    target = mutation_policy_env.root / "private.txt"
    original = b"do not disclose this original content\n"
    target.write_bytes(original)
    monkeypatch.setattr(SnapshotStore, "capture", _reject_snapshot_capture)

    target_key = os.path.normcase(os.path.abspath(target))
    original_path_open = Path.open
    original_os_open = os.open

    def is_target(path) -> bool:
        try:
            return os.path.normcase(os.path.abspath(os.fspath(path))) == target_key
        except (TypeError, ValueError, OSError):
            return False

    def guarded_path_open(path, mode="r", *args, **kwargs):
        if is_target(path):
            pytest.fail("dry-run read target contents without READ permission")
        return original_path_open(path, mode, *args, **kwargs)

    def guarded_os_open(path, flags, *args, **kwargs):
        if is_target(path) and (flags & os.O_ACCMODE) == os.O_RDONLY:
            pytest.fail("dry-run fingerprinted target contents without READ permission")
        return original_os_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as deny_reads:
        deny_reads.setattr(Path, "open", guarded_path_open)
        deny_reads.setattr(os, "open", guarded_os_open)
        raw_result = file_ops.file_write(
            str(target),
            "replacement content\n",
            overwrite=True,
            dry_run=True,
        )
    result = json.loads(raw_result)

    assert result["status"] == "ok"
    assert result["dry_run"] is True
    preview = result["preview"]["preview"]
    assert preview["kind"] == "summary"
    assert preview["reason"] == "read_permission_required"
    assert "do not disclose this original content" not in repr(result)
    assert "replacement content" not in repr(result)
    assert target.read_bytes() == original


def test_native_move_captures_snapshot_and_verifies_same_volume_rename(
    mutation_policy_env,
    monkeypatch,
    tmp_path,
):
    mutation_policy_env.grant(read=True, write=True)
    source = mutation_policy_env.root / "source.txt"
    destination = mutation_policy_env.root / "destination.txt"
    contents = b"move this file safely\n"
    source.write_bytes(contents)
    store = SnapshotStore(tmp_path / "snapshots")
    monkeypatch.setattr(file_ops, "_p3_snapshot_store", lambda: store)

    class Execution:
        run_id = "run-preview-move"
        operation_id = "op-preview-move"
        approved_ticket_id = ""
        approved_binding_digest = ""

        def __init__(self):
            self.snapshot_ref = None
            self.effect_class = "opaque_effect"
            self.no_effect_reasons = []

        def native_file_no_effect(self, reason_code="operation_failed"):
            self.no_effect_reasons.append(reason_code)

        def promote_native_file_mutation(self, *, snapshot_ref, **_kwargs):
            self.snapshot_ref = snapshot_ref
            self.effect_class = "verifiable_write"

        def assert_native_file_effect_allowed(self):
            assert self.effect_class == "verifiable_write"

        def verify_native_file_mutation(self, verifier):
            return verifier.verify({
                "run_id": self.run_id,
                "operation_id": self.operation_id,
                "operation_kind": "file_move",
                "effect_class": self.effect_class,
                "status": "executing",
                "snapshot_ref": self.snapshot_ref,
            })

    execution = Execution()
    monkeypatch.setattr(file_ops, "_native_file_execution", lambda: execution)

    result = json.loads(file_ops.fs_move(str(source), str(destination)))

    assert result["status"] == "ok"
    assert result["verification"]["verification_result"] == "completed"
    assert not source.exists()
    assert destination.read_bytes() == contents
    assert execution.snapshot_ref is not None
    assert list(store.root.glob("*.snap")) == [store.root / f"{execution.snapshot_ref}.snap"]


def test_fs_copy_keeps_source_in_place(mutation_policy_env):
    mutation_policy_env.grant(read=True, write=True)
    source = mutation_policy_env.root / "copy-source.txt"
    destination = mutation_policy_env.root / "copy-destination.txt"
    contents = b"copy leaves its source\n"
    source.write_bytes(contents)

    result = json.loads(file_ops.fs_copy(str(source), str(destination)))

    assert result["status"] == "ok"
    assert source.read_bytes() == contents
    assert destination.read_bytes() == contents
