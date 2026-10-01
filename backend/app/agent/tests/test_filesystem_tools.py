import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.settings_store import AgentSettings, PathRule, save_agent_settings
from app.agent.tool_registry import ToolRegistry
from app.agent.run_context import (
    reset_current_control_session_id,
    reset_current_conversation_id,
    reset_current_principal_id,
    set_current_control_session_id,
    set_current_conversation_id,
    set_current_principal_id,
)
from app.agent.response_attachments import register_attachment_path
from app.skills.filesystem import file_ops
from app.skills.filesystem import tools as filesystem_tools


@pytest.fixture
def policy_env(tmp_path, monkeypatch):
    import app.agent.controller_policy as cp

    policy_dir = tmp_path / "policy"
    policy_dir.mkdir()
    monkeypatch.setattr(cp, "_POLICY_DIR", policy_dir)
    monkeypatch.setattr(cp, "_POLICY_FILE", policy_dir / "controller_policy.md")
    monkeypatch.setattr(cp, "_ALLOWLIST_FILE", policy_dir / "allowlisted_apps.md")

    root = tmp_path / "workspace"
    root.mkdir()

    def save_settings(settings_data: AgentSettings) -> None:
        save_agent_settings(settings_data, settings_path=policy_dir.parent / "settings.json")

    return SimpleNamespace(root=root, save_settings=save_settings)


def _settings_for_root(root, *, mode: str = "full_access", blocked_roots: list[str] | None = None) -> AgentSettings:
    settings_data = AgentSettings()
    settings_data.permissions.mode = mode
    settings_data.permissions.blocked_roots = blocked_roots or []
    settings_data.permissions.path_rules = [
        PathRule(path=str(root), read=True, write=True, delete=True, launch=False, enabled=True)
    ]
    return settings_data


def test_filesystem_read_write_list_and_search_are_policy_gated(policy_env):
    policy_env.save_settings(_settings_for_root(policy_env.root))
    target = policy_env.root / "notes.txt"

    write_result = json.loads(file_ops.file_write(str(target), "alpha\nbeta", overwrite=True))
    read_result = json.loads(file_ops.file_read(str(target)))
    list_result = json.loads(file_ops.file_list(str(policy_env.root)))
    search_result = json.loads(file_ops.file_search(str(policy_env.root), "beta"))
    patch_result = json.loads(file_ops.file_patch(str(target), "beta", "gamma"))
    hash_result = json.loads(file_ops.file_hash(str(target)))
    tree_result = json.loads(file_ops.file_tree(str(policy_env.root)))

    assert write_result["status"] == "ok"
    assert write_result["operation"] == "write"
    assert read_result["content"] == "alpha\nbeta"
    assert read_result["operation"] == "read"
    assert list_result["entries"][0]["path"] == str(target)
    assert search_result["matches"][0]["line"] == 2
    assert patch_result["replacements"] == 1
    assert hash_result["algorithm"] == "sha256"
    assert tree_result["entries"][0]["relative_path"] == "notes.txt"


def test_filesystem_read_accepts_scoped_attachment_handle(policy_env):
    policy_env.save_settings(_settings_for_root(policy_env.root))
    target = policy_env.root / "upload.txt"
    target.write_text("attached payload", encoding="utf-8")
    register_attachment_path(
        "upload-1",
        target,
        control_session_id="session-a",
        principal_id="session-a",
        conversation_id="conv-a",
    )
    session_token = set_current_control_session_id("session-a")
    principal_token = set_current_principal_id("session-a")
    conversation_token = set_current_conversation_id("conv-a")
    try:
        result = json.loads(file_ops.file_read("attachment://upload-1"))
    finally:
        reset_current_conversation_id(conversation_token)
        reset_current_principal_id(principal_token)
        reset_current_control_session_id(session_token)

    assert result["status"] == "ok"
    assert result["content"] == "attached payload"
    assert result["path"] == str(target.resolve(strict=False))


def test_filesystem_mutation_helpers_are_policy_gated(policy_env):
    policy_env.save_settings(_settings_for_root(policy_env.root))
    source = policy_env.root / "source.txt"
    copied = policy_env.root / "nested" / "copied.txt"
    moved = policy_env.root / "moved.txt"
    source.write_text("payload", encoding="utf-8")

    mkdir_result = json.loads(file_ops.fs_mkdir(str(policy_env.root / "nested")))
    copy_result = json.loads(file_ops.fs_copy(str(source), str(copied)))
    move_result = json.loads(file_ops.fs_move(str(copied), str(moved)))
    rename_result = json.loads(file_ops.fs_rename(str(moved), "renamed.txt"))

    assert mkdir_result["operation"] == "mkdir"
    assert copy_result["operation"] == "copy"
    assert move_result["operation"] == "move"
    assert rename_result["operation"] == "rename"
    assert (policy_env.root / "renamed.txt").read_text(encoding="utf-8") == "payload"


def test_filesystem_same_path_move_and_rename_are_noops(policy_env):
    policy_env.save_settings(_settings_for_root(policy_env.root))
    target = policy_env.root / "same.txt"
    target.write_text("payload", encoding="utf-8")

    move_result = json.loads(file_ops.fs_move(str(target), str(target), overwrite=True))
    rename_result = json.loads(file_ops.fs_rename(str(target), target.name, overwrite=True))

    assert move_result["status"] == "ok"
    assert move_result["same_path"] is True
    assert rename_result["status"] == "ok"
    assert rename_result["same_path"] is True
    assert target.read_text(encoding="utf-8") == "payload"


def test_filesystem_registers_canonical_visible_surface_and_hidden_aliases():
    registry = ToolRegistry()
    filesystem_tools.register_tools(registry)

    visible_names = {tool["name"] for tool in registry.get_all_tools(visible_only=True)}
    all_names = {tool["name"] for tool in registry.get_all_tools()}

    assert {
        "fs_stat",
        "fs_list",
        "fs_tree",
        "fs_read",
        "fs_search",
        "fs_hash",
        "fs_write",
        "fs_append",
        "fs_patch",
        "fs_mkdir",
        "fs_copy",
        "fs_move",
        "fs_rename",
        "fs_delete",
        "explorer_open",
        "explorer_reveal",
    } <= visible_names
    assert "file_read" in all_names
    assert "file_read" not in visible_names
    assert "copy" not in visible_names


def test_filesystem_blocks_configured_blocked_roots(policy_env):
    blocked = policy_env.root / "blocked"
    blocked.mkdir()
    target = blocked / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    policy_env.save_settings(_settings_for_root(policy_env.root, blocked_roots=[str(blocked)]))

    result = json.loads(file_ops.file_read(str(target)))

    assert result["status"] == "blocked"
    assert result["reason_code"] == "blocked_root"


def test_filesystem_unknown_path_requires_access_grant(policy_env):
    settings_data = AgentSettings()
    settings_data.permissions.mode = "custom"
    settings_data.permissions.blocked_roots = []
    settings_data.permissions.path_rules = []
    policy_env.save_settings(settings_data)
    target = policy_env.root.parent / "outside.txt"
    target.write_text("secret", encoding="utf-8")

    result = json.loads(file_ops.file_read(str(target)))

    assert result["status"] == "pending_access_grant"
    assert result["reason_code"] == "access_grant_required"


def test_filesystem_write_respects_default_confirmation(policy_env, monkeypatch):
    policy_env.save_settings(_settings_for_root(policy_env.root, mode="default"))
    monkeypatch.setattr(file_ops, "create_ticket", lambda **_kwargs: SimpleNamespace(id="ticket-123"))

    result = json.loads(file_ops.file_write(str(policy_env.root / "new.txt"), "hello", overwrite=True))

    assert result["status"] == "pending_approval"
    assert result["ticket_id"] == "ticket-123"


def test_filesystem_write_uses_canonical_approval_tool_name(policy_env, monkeypatch):
    policy_env.save_settings(_settings_for_root(policy_env.root, mode="default"))
    captured = {}

    def fake_create_ticket(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id="ticket-123")

    monkeypatch.setattr(file_ops, "create_ticket", fake_create_ticket)

    result = json.loads(file_ops.file_write(str(policy_env.root / "new.txt"), "hello", overwrite=True))

    assert result["status"] == "pending_approval"
    assert captured["tool_name"] == "fs_write"


def test_batch_results_stops_on_error():
    from app.skills.filesystem.tools import _batch_results

    calls = []

    def fake(item):
        calls.append(item)
        if item == "bad":
            return '{"status": "error", "error": "bad"}'
        return '{"status": "ok"}'

    result = json.loads(_batch_results(["a", "bad", "c"], fake))

    assert len(result["results"]) == 2
    assert calls == ["a", "bad"]


def test_batch_results_surfaces_pending_permission():
    from app.skills.filesystem.tools import _batch_results

    result = json.loads(
        _batch_results(
            ["needs-grant"],
            lambda _item: '{"status": "pending_access_grant", "ticket_id": "ticket-123"}',
        )
    )

    assert result["status"] == "pending_access_grant"
    assert result["ticket_id"] == "ticket-123"
    assert result["count"] == 1



def test_filesystem_rejects_parent_traversal_segments(policy_env):
    policy_env.save_settings(_settings_for_root(policy_env.root))

    result = json.loads(file_ops.file_read(str(policy_env.root / "nested" / ".." / "notes.txt")))

    assert result["status"] == "error"
    assert result["reason_code"] == "path_traversal_rejected"


def test_filesystem_write_enforces_byte_budget(policy_env, monkeypatch):
    policy_env.save_settings(_settings_for_root(policy_env.root))
    monkeypatch.setattr(file_ops, "_MAX_WRITE_BYTES", 4)

    result = json.loads(file_ops.file_write(str(policy_env.root / "large.txt"), "12345", overwrite=True))

    assert result["status"] == "error"
    assert result["reason_code"] == "write_size_limit_exceeded"


def test_filesystem_recursive_copy_rejects_symlink(policy_env):
    policy_env.save_settings(_settings_for_root(policy_env.root))
    source = policy_env.root / "source"
    source.mkdir()
    (source / "payload.txt").write_text("payload", encoding="utf-8")
    link = source / "linked.txt"
    try:
        link.symlink_to(source / "payload.txt")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    result = json.loads(file_ops.fs_copy(str(source), str(policy_env.root / "copy")))

    assert result["status"] == "error"
    assert result["reason_code"] == "symlink_target_rejected"


@pytest.mark.parametrize(
    ("operation", "pattern_kwarg"),
    [("list", "pattern"), ("search", "glob")],
)
def test_filesystem_glob_pattern_cannot_escape_gated_base(policy_env, tmp_path, operation, pattern_kwarg):
    policy_env.save_settings(_settings_for_root(policy_env.root))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("needle", encoding="utf-8")

    call = file_ops.file_list if operation == "list" else file_ops.file_search
    args = (str(policy_env.root),) if operation == "list" else (str(policy_env.root), "needle")
    for pattern in ("../outside/*", "..\outside\*", "**/../../outside/*", str(outside / "*")):
        result = json.loads(call(*args, **{pattern_kwarg: pattern}))
        assert result["status"] == "error", pattern
        assert result["reason_code"] in {"path_traversal_rejected", "glob_pattern_rejected"}


def test_filesystem_search_skips_links_that_leave_gated_base(policy_env, tmp_path):
    policy_env.save_settings(_settings_for_root(policy_env.root))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("needle", encoding="utf-8")
    try:
        (policy_env.root / "linked.txt").symlink_to(outside / "secret.txt")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    result = json.loads(file_ops.file_search(str(policy_env.root), "needle"))

    assert result["status"] == "ok"
    assert result["count"] == 0


@pytest.mark.parametrize("operation", ["move", "rename"])
def test_filesystem_overwrite_of_directory_respects_delete_disabled(policy_env, operation):
    settings_data = _settings_for_root(policy_env.root, mode="custom")
    settings_data.permissions.allow_delete = False
    settings_data.permissions.confirmations.mutate = False
    policy_env.save_settings(settings_data)
    source = policy_env.root / "source"
    source.mkdir()
    destination = policy_env.root / "victim"
    destination.mkdir()
    (destination / "keep.txt").write_text("keep", encoding="utf-8")

    if operation == "move":
        result = json.loads(file_ops.fs_move(str(source), str(destination), overwrite=True))
    else:
        result = json.loads(file_ops.fs_rename(str(source), "victim", overwrite=True))

    assert result["status"] == "blocked"
    assert result["reason_code"] == "delete_disabled"
    assert (destination / "keep.txt").exists()


def test_filesystem_cannot_rewrite_agent_settings(policy_env):
    import app.agent.controller_policy as cp

    policy_env.save_settings(_settings_for_root(policy_env.root))
    settings_path = cp._settings_json_path()
    original = settings_path.read_text(encoding="utf-8")

    result = json.loads(file_ops.file_write(str(settings_path), "{}", overwrite=True))

    assert result["status"] == "blocked"
    assert result["reason_code"] == "protected_control_path"
    assert settings_path.read_text(encoding="utf-8") == original


def _make_tree(root, paths):
    for relative in paths:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")


def test_filesystem_tree_never_scans_below_requested_depth(policy_env, monkeypatch):
    policy_env.save_settings(_settings_for_root(policy_env.root))
    _make_tree(policy_env.root, ["a/b/c/d/deep.txt", "a/top.txt", "z.txt"])
    scanned: list[str] = []
    real_scandir = file_ops.os.scandir

    def recording_scandir(path):
        scanned.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(file_ops.os, "scandir", recording_scandir)

    result = json.loads(file_ops.file_tree(str(policy_env.root), depth=2))

    assert result["status"] == "ok"
    assert result["truncated"] is False
    assert [entry["relative_path"] for entry in result["entries"]] == [
        "a",
        str(Path("a") / "b"),
        str(Path("a") / "top.txt"),
        "z.txt",
    ]
    assert sorted(scanned) == sorted([str(policy_env.root), str(policy_env.root / "a")])


def test_filesystem_tree_reports_limit_and_time_budget_truncation(policy_env, monkeypatch):
    policy_env.save_settings(_settings_for_root(policy_env.root))
    _make_tree(policy_env.root, [f"file-{index}.txt" for index in range(5)])

    limited = json.loads(file_ops.file_tree(str(policy_env.root), limit=3))
    monkeypatch.setattr(file_ops, "_WALK_TIME_BUDGET_SECONDS", -1.0)
    timed_out = json.loads(file_ops.file_tree(str(policy_env.root)))

    assert limited["count"] == 3
    assert limited["truncated"] is True
    assert limited["truncated_reason"] == "limit"
    assert timed_out["truncated"] is True
    assert timed_out["truncated_reason"] == "time_budget"


def test_filesystem_tree_does_not_enter_blocked_roots_under_granted_path(policy_env):
    blocked = policy_env.root / "private"
    _make_tree(policy_env.root, ["private/secret.txt", "public.txt"])
    policy_env.save_settings(_settings_for_root(policy_env.root, blocked_roots=[str(blocked)]))

    result = json.loads(file_ops.file_tree(str(policy_env.root)))

    entries = {entry["relative_path"]: entry for entry in result["entries"]}
    assert set(entries) == {"private", "public.txt"}
    assert entries["private"]["restricted"] is True


def test_filesystem_tree_lists_but_does_not_follow_directory_links(policy_env):
    policy_env.save_settings(_settings_for_root(policy_env.root))
    _make_tree(policy_env.root, ["real/inner.txt"])
    try:
        (policy_env.root / "linked").symlink_to(policy_env.root / "real", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    result = json.loads(file_ops.file_tree(str(policy_env.root)))

    relative_paths = {entry["relative_path"] for entry in result["entries"]}
    assert "linked" in relative_paths
    assert str(Path("real") / "inner.txt") in relative_paths
    assert str(Path("linked") / "inner.txt") not in relative_paths


def test_filesystem_directory_walk_tools_have_timeout_backstop():
    registry = ToolRegistry()
    filesystem_tools.register_tools(registry)
    tools = {tool["name"]: tool for tool in registry.get_all_tools()}

    for name in ("fs_list", "fs_tree", "fs_search", "file_tree"):
        assert tools[name]["timeout_seconds"] == 60.0, name
    assert "timeout_seconds" not in tools["fs_read"]


def test_filesystem_tree_does_not_follow_windows_junctions(policy_env):
    winapi = pytest.importorskip("_winapi")
    policy_env.save_settings(_settings_for_root(policy_env.root))
    _make_tree(policy_env.root, ["real/inner.txt"])
    winapi.CreateJunction(str(policy_env.root / "real"), str(policy_env.root / "junction"))

    result = json.loads(file_ops.file_tree(str(policy_env.root)))

    relative_paths = {entry["relative_path"] for entry in result["entries"]}
    assert "junction" in relative_paths
    assert str(Path("junction") / "inner.txt") not in relative_paths


@pytest.mark.parametrize(
    ("pattern", "recursive", "expected"),
    [
        ("*.txt", False, ["top.txt"]),
        ("*.txt", True, ["top.txt", "sub/nested.txt", "sub/deeper/deep.txt"]),
        ("sub/*.txt", False, ["sub/nested.txt"]),
        ("**/deep*", False, ["sub/deeper", "sub/deeper/deep.txt"]),
        ("**", False, ["sub", "sub/deeper"]),
    ],
)
def test_filesystem_list_matches_glob_semantics(policy_env, pattern, recursive, expected):
    policy_env.save_settings(_settings_for_root(policy_env.root))
    _make_tree(policy_env.root, ["top.txt", "top.md", "sub/nested.txt", "sub/deeper/deep.txt"])

    result = json.loads(file_ops.file_list(str(policy_env.root), pattern=pattern, recursive=recursive))

    assert sorted(entry["relative_path"] for entry in result["entries"]) == sorted(str(Path(p)) for p in expected)


def test_filesystem_walk_stops_when_the_tool_call_is_cancelled(policy_env):
    from app.agent.tool_cancellation import cancel_call, reset_current_call_id, set_current_call_id

    policy_env.save_settings(_settings_for_root(policy_env.root))
    _make_tree(policy_env.root, [f"file-{index}.txt" for index in range(5)])
    token = set_current_call_id("call-walk")
    try:
        with file_ops._WalkBudget() as budget:
            walked = []
            for item in file_ops._walk(policy_env.root, budget):
                walked.append(item)
                assert cancel_call("call-walk") is True
    finally:
        reset_current_call_id(token)

    assert len(walked) == 1
    assert budget.exhausted_reason == "cancelled"
    assert cancel_call("call-walk") is False


def test_filesystem_search_does_not_read_blocked_roots_under_granted_path(policy_env):
    blocked = policy_env.root / "private"
    _make_tree(policy_env.root, ["private/secret.txt", "public.txt"])
    (blocked / "secret.txt").write_text("needle", encoding="utf-8")
    (policy_env.root / "public.txt").write_text("needle", encoding="utf-8")
    policy_env.save_settings(_settings_for_root(policy_env.root, blocked_roots=[str(blocked)]))

    result = json.loads(file_ops.file_search(str(policy_env.root), "needle"))

    assert [match["path"] for match in result["matches"]] == [str(policy_env.root / "public.txt")]
