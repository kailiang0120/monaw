import json
from types import SimpleNamespace

import pytest

from app.agent.settings_store import AgentSettings, PathRule, save_agent_settings
from app.agent.tool_registry import ToolRegistry
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
    settings_data.permissions.mode = "default"
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
