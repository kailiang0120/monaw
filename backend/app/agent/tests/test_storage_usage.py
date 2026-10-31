"""Storage usage by category and the cleanups that are safe to run."""
import asyncio
from pathlib import Path

import pytest

from app.agent import storage_usage as storage
from app.api.routes import storage as storage_route


def _write(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "monaw"
    runtime = home / "runtime"
    workspace = home / "workspace"
    for path in (home, runtime, workspace):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage, "MONAW_HOME_DIR", home)
    monkeypatch.setattr(storage, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(storage, "WORKSPACE_DIR", workspace)
    monkeypatch.setattr(storage, "_database_paths", lambda: [runtime / "agent.db", runtime / "agent.db-wal"])
    from app.agent.observability import recorder
    monkeypatch.setattr(recorder, "BACKEND_LOG_PATH", runtime / "backend.log")
    return home


def _category(usage: dict, category_id: str) -> dict:
    return next(item for item in usage["categories"] if item["id"] == category_id)


def test_usage_adds_up_by_category(home):
    runtime = home / "runtime"
    _write(runtime / "agent.db", 1000)
    _write(runtime / "agent.db-wal", 200)
    _write(runtime / "observability" / "observability.sqlite3", 3000)
    _write(runtime / "backend.log", 400)
    _write(runtime / "exec" / "run-1" / "out.txt", 500)
    _write(runtime / "electron" / "Cache" / "data_1", 600)
    _write(home / "codex-account" / "cache" / "a", 700)
    _write(runtime / "settings.json", 50)

    usage = storage.storage_usage()

    assert usage["total_bytes"] == 6450
    assert _category(usage, "chats")["bytes"] == 1200
    assert _category(usage, "activity")["bytes"] == 3000
    assert _category(usage, "logs")["bytes"] == 400
    assert _category(usage, "outputs")["bytes"] == 500
    assert _category(usage, "app_cache")["clean"] == "app"
    assert _category(usage, "account") | {"bytes": 700} == _category(usage, "account")
    assert _category(usage, "account")["clean"] is None
    assert _category(usage, "other")["bytes"] == 50
    assert not any(item["id"] == "legacy_speech" for item in usage["categories"])


def test_browser_cleanup_keeps_sign_ins(home):
    profile = home / "browser" / "profiles" / "managed"
    cookies = _write(profile / "Default" / "Network" / "Cookies", 300)
    _write(profile / "Default" / "Cache" / "f_000001", 2000)
    _write(profile / "BrowserMetrics" / "metrics.pma", 1000)

    before = _category(storage.storage_usage(), "browser")
    result = storage.clean_storage("browser")

    assert (before["bytes"], before["cleanable_bytes"]) == (3300, 3000)
    assert result["freed_bytes"] == 3000
    assert cookies.read_bytes() == b"x" * 300
    assert _category(result["usage"], "browser")["cleanable_bytes"] == 0


def test_log_cleanup_empties_the_open_backend_log(home):
    runtime = home / "runtime"
    log = _write(runtime / "backend.log", 900)
    _write(runtime / "backend.log.1", 500)
    _write(runtime / "debug-abc123.log", 100)
    _write(home / "browser" / "screenshots" / "shot.png", 300)

    result = storage.clean_storage("logs")

    assert log.exists() and log.stat().st_size == 0
    assert result["freed_bytes"] == 1800
    assert not (runtime / "backend.log.1").exists()
    assert not (home / "browser" / "screenshots" / "shot.png").exists()


def test_outputs_and_uploads_are_emptied_but_their_folders_stay(home):
    runtime = home / "runtime"
    _write(runtime / "exec" / "run" / "out.txt", 100)
    _write(runtime / "uploads" / "conv" / "id" / "photo.jpg", 400)

    assert storage.clean_storage("outputs")["freed_bytes"] == 100
    assert storage.clean_storage("uploads")["freed_bytes"] == 400
    assert (runtime / "exec").is_dir() and (runtime / "uploads").is_dir()


def test_old_voice_model_is_listed_only_while_it_exists(home):
    _write(home / "models" / "faster-whisper" / "base" / "model.bin", 5000)

    assert _category(storage.storage_usage(), "legacy_speech")["cleanable_bytes"] == 5000
    storage.clean_storage("legacy_speech")
    assert not any(item["id"] == "legacy_speech" for item in storage.storage_usage()["categories"])


def test_chats_are_not_deleted_during_a_reply(home, monkeypatch):
    from app.agent import runtime
    monkeypatch.setattr(runtime, "active_runtime_run_count", lambda: 1)

    with pytest.raises(storage.StorageCleanupError, match="current reply"):
        storage.clean_storage("chats")


def test_deleting_chats_removes_each_conversation_and_compacts(home, monkeypatch):
    from app.agent import database, memory_manager, runtime
    deleted, compacted = [], []
    monkeypatch.setattr(runtime, "active_runtime_run_count", lambda: 0)
    monkeypatch.setattr(memory_manager, "peek_memory_manager", lambda: None)
    monkeypatch.setattr(memory_manager, "list_conversation_metadata", lambda: [{"id": "a"}, {"id": "b"}])
    monkeypatch.setattr(memory_manager, "delete_conversation_file", lambda conv_id: deleted.append(conv_id) or True)
    monkeypatch.setattr(database, "get_db", lambda: type("Db", (), {"compact": lambda self: compacted.append(True)})())

    storage.clean_storage("chats")

    assert deleted == ["a", "b"]
    assert compacted == [True]


def test_paths_outside_monaw_are_never_removed(home, tmp_path):
    outside = _write(tmp_path / "outside" / "keep.txt", 10)

    storage._remove(outside)
    storage._remove(outside.parent)

    assert outside.exists()


def test_routes_report_unknown_and_busy_categories(home, monkeypatch):
    with pytest.raises(storage_route.HTTPException) as unknown:
        asyncio.run(storage_route.clean_storage_category("account"))
    assert unknown.value.status_code == 404

    from app.agent import runtime
    monkeypatch.setattr(runtime, "active_runtime_run_count", lambda: 2)
    with pytest.raises(storage_route.HTTPException) as busy:
        asyncio.run(storage_route.clean_storage_category("chats"))
    assert busy.value.status_code == 409
