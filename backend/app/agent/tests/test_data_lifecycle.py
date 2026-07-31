from types import SimpleNamespace

from app.agent import access_grant_broker, approval_broker, data_lifecycle, response_attachments
from app.agent.database import Database
from app.agent.observability.recorder import ObservabilityRecorder
from app.agent.response_attachments import register_attachment_path


def test_delete_runtime_data_clears_observability_support_and_retained_content(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "runtime"
    home_dir = tmp_path / "home"
    runtime_dir.mkdir()
    home_dir.mkdir()

    db = Database(runtime_dir / "agent.db")
    db.init_db()
    db.create_scheduled_task({
        "id": "task-1",
        "title": "Daily",
        "prompt": "summarize",
        "schedule_kind": "interval",
    })
    run_id = db.create_scheduled_task_run(
        task_id="task-1",
        conversation_id="conv-scheduled",
        started_at="2026-05-23T00:00:00+00:00",
        status="complete",
    )
    db.update_scheduled_task_run(run_id, final_text="secret scheduled output", error="secret error")

    recorder = ObservabilityRecorder(root=runtime_dir / "observability", capture_sensitive_content=True)
    observed_run_id = recorder.start_run(conversation_id="conv-1", user_message="secret prompt")
    recorder.finish_run(run_id=observed_run_id, status="complete", final_output="secret answer")
    assert recorder.list_runs()

    approval_broker._APPROVALS_DIR = runtime_dir / "approvals"  # noqa: SLF001
    approval_broker._TICKETS_FILE = approval_broker._APPROVALS_DIR / "tickets.jsonl"  # noqa: SLF001
    approval_broker._APPROVAL_LOG = runtime_dir / "policy" / "approval_log.md"  # noqa: SLF001
    approval_broker.clear_all_tickets()
    approval_broker.create_ticket(conversation_id="conv-1", tool_name="filesystem_write", payload={"path": "secret"})
    assert approval_broker.get_pending_tickets()

    monkeypatch.setattr(access_grant_broker, "get_db", lambda: db)
    access_grant_broker.clear_all_grants()
    access_grant_broker.create_grant_ticket(
        conversation_id="conv-1",
        target_type="path",
        target_identifier=str(runtime_dir / "allowed.txt"),
    )
    assert access_grant_broker.get_pending_grants()

    attachment_file = runtime_dir / "upload.txt"
    attachment_file.write_text("attached secret", encoding="utf-8")
    monkeypatch.setattr(response_attachments, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(response_attachments, "_ATTACHMENT_REGISTRY_PATH", runtime_dir / "attachment_registry.json")
    response_attachments._ATTACHMENT_REGISTRY.clear()  # noqa: SLF001
    response_attachments._ATTACHMENT_REGISTRY_LOADED = False  # noqa: SLF001
    register_attachment_path("attachment-1", attachment_file)
    assert response_attachments.attachment_registry_stats()["attachment_registry_items"] == 1

    backend_log = runtime_dir / "backend.log"
    backend_log.write_text("password=hunter2", encoding="utf-8")
    diagnostics_file = home_dir / "browser" / "screenshots" / "shot.txt"
    diagnostics_file.parent.mkdir(parents=True)
    diagnostics_file.write_text("browser diagnostic", encoding="utf-8")
    memory_root = home_dir / "memory"
    memory_root.mkdir()
    (memory_root / "memory.md").write_text("remember this", encoding="utf-8")

    monkeypatch.setattr(data_lifecycle, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(data_lifecycle, "MONAW_HOME_DIR", home_dir)
    monkeypatch.setattr(data_lifecycle, "get_db", lambda: db)
    monkeypatch.setattr(data_lifecycle, "get_observability_recorder", lambda: recorder)
    monkeypatch.setattr(data_lifecycle, "get_long_term_memory", lambda: SimpleNamespace(root=memory_root))
    monkeypatch.setattr(data_lifecycle, "reset_long_term_memory", lambda: None)

    result = data_lifecycle.delete_runtime_data()

    assert result["ok"] is True
    assert recorder.list_runs() == []
    assert approval_broker.get_pending_tickets() == []
    assert access_grant_broker.get_pending_grants() == []
    assert response_attachments.attachment_registry_stats()["attachment_registry_items"] == 0
    assert not attachment_file.exists()
    assert not backend_log.exists()
    assert not diagnostics_file.exists()
    assert not memory_root.exists()
    assert db.list_scheduled_task_runs("task-1")[0]["final_text"] == ""
    assert db.list_scheduled_task_runs("task-1")[0]["error"] == ""
