import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.agent import conversation_workspace as workspace
from app.agent import database, memory_manager, workspace_instructions
from app.agent.runtime import AgentRuntime
from app.agent.steering import SteeringInbox, register_steering, unregister_steering
from app.main import app
from app.skills.exec import tools as exec_tools
from app.skills.filesystem import file_ops


@pytest.fixture
def db(tmp_path, monkeypatch):
    instance = database.Database(tmp_path / "agent.db")
    instance.init_db()
    monkeypatch.setattr(database, "_db", instance)
    monkeypatch.setattr(memory_manager, "peek_memory_manager", lambda: None)
    yield instance
    instance.close()


def test_saved_folder_survives_reopen_and_can_be_reset(db, tmp_path):
    client = TestClient(app)
    project = tmp_path / "project"
    project.mkdir()
    created = client.post("/api/conversations", json={"title": "Project", "workspace_dir": str(project)})
    assert created.status_code == 200
    conv_id = created.json()["id"]
    db.close()
    assert db.get_conversation(conv_id)["workspace_dir"] == str(project.resolve())
    listed = client.get("/api/conversations").json()
    assert listed[0]["workspace_dir"] == str(project.resolve())
    renamed = client.patch(f"/api/conversations/{conv_id}", json={"title": "Renamed"})
    assert renamed.json()["workspace_dir"] == str(project.resolve())
    reset = client.patch(f"/api/conversations/{conv_id}", json={"workspace_dir": ""})
    assert reset.status_code == 200
    assert reset.json()["workspace_dir"] == ""
    assert reset.json()["title"] == "Renamed"


def test_older_conversations_keep_default_after_migration(db):
    db.conn.execute("ALTER TABLE conversations DROP COLUMN workspace_dir")
    db.conn.execute("DELETE FROM schema_migrations WHERE version = 13")
    db.conn.execute("INSERT INTO conversations (id, title, created_at, updated_at) VALUES ('old', 'Old chat', 'now', 'now')")
    db.conn.commit()
    db.init_db()
    db.init_db()
    assert db.get_conversation("old")["workspace_dir"] == ""
    assert db.list_conversations()[0]["workspace_dir"] == ""


def test_invalid_folders_are_rejected_without_changing_saved_folder(db, tmp_path):
    client = TestClient(app)
    db.create_conversation("chat", "Chat", str(tmp_path))
    file = tmp_path / "file.txt"
    file.write_text("test")
    for path in ("relative/path", str(tmp_path / "missing"), str(file)):
        assert client.patch("/api/conversations/chat", json={"workspace_dir": path}).status_code == 422
        assert client.post("/api/conversations", json={"workspace_dir": path}).status_code == 422
        assert client.post("/api/conversations/workspace-validation", json={"workspace_dir": path}).status_code == 422
    validation = client.post("/api/conversations/workspace-validation", json={"workspace_dir": str(tmp_path)})
    assert validation.json() == {"workspace_dir": str(tmp_path.resolve())}
    assert len(db.list_conversations()) == 1
    assert db.get_conversation("chat")["workspace_dir"] == str(tmp_path)
    assert client.patch("/api/conversations/chat", json={}).status_code == 422


def test_running_chat_cannot_change_folder(db, tmp_path):
    db.create_conversation("chat", "Chat")
    inbox = SteeringInbox("chat", "session")
    register_steering(inbox)
    try:
        response = TestClient(app).patch("/api/conversations/chat", json={"workspace_dir": str(tmp_path)})
        assert response.status_code == 409
        assert db.get_conversation("chat")["workspace_dir"] == ""
    finally:
        unregister_steering(inbox)


def test_first_message_saves_folder_before_starting_agent(db, tmp_path, monkeypatch):
    from app.agent import runtime

    calls = []

    async def fake_stream(**kwargs):
        calls.append(db.get_conversation(kwargs["conversation_id"]))
        yield {"event": "done", "data": {"conversation_id": kwargs["conversation_id"]}}

    monkeypatch.setattr(runtime, "run_agent_stream", fake_stream)
    client = TestClient(app)
    response = client.post("/api/chat", json={"message": "Build my project", "workspace_dir": str(tmp_path)})
    assert response.status_code == 200
    assert calls[0]["workspace_dir"] == str(tmp_path.resolve())
    assert calls[0]["title"] == "Build my project"
    conv_id = calls[0]["id"]
    response = client.post("/api/chat", json={"message": "Continue", "conversation_id": conv_id})
    assert response.status_code == 200
    assert calls[1]["workspace_dir"] == str(tmp_path.resolve())
    response = client.post("/api/chat", json={"message": "Continue", "conversation_id": conv_id, "workspace_dir": ""})
    assert response.status_code == 409
    assert len(calls) == 2


def test_concurrent_runtime_turns_keep_commands_files_and_instructions_in_their_folder(db, tmp_path, monkeypatch):
    default = tmp_path / "default"
    default.mkdir()
    monkeypatch.setattr(workspace_instructions, "WORKSPACE_DIR", default)
    projects = []
    for name in ("one", "two"):
        project = tmp_path / name
        project.mkdir()
        (project / "AGENTS.md").write_text(f"Instruction for {name}")
        (project / "notes.txt").write_text(name)
        db.create_conversation(name, name, str(project))
        projects.append(project)

    async def fake_turn(_message, conversation_id, *, system_prompt, **_kwargs):
        await asyncio.sleep(0)
        assert workspace.current_workspace_dir() == tmp_path / conversation_id
        assert exec_tools._effective_workdir("") == str(tmp_path / conversation_id)
        assert exec_tools._effective_workdir("subdir") == str(tmp_path / conversation_id / "subdir")
        assert file_ops._resolve_path("notes.txt").read_text() == conversation_id
        assert str(file_ops._policy_target_path("notes.txt")) == str(tmp_path / conversation_id / "notes.txt")
        prompt = system_prompt()
        assert f"Instruction for {conversation_id}" in prompt
        other = "two" if conversation_id == "one" else "one"
        assert f"Instruction for {other}" not in prompt
        assert str(tmp_path / conversation_id) in prompt
        assert file_ops._resolve_path(str(tmp_path / other)) == tmp_path / other
        yield {"event": "done", "data": {"conversation_id": conversation_id}}

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime._active_runs = 0
    runtime._retired = False
    runtime.turn_loop = SimpleNamespace(run=fake_turn)
    runtime.settings = SimpleNamespace()
    runtime.skills = []
    runtime.tool_registry = SimpleNamespace(revision=1, get_all_tools=lambda **_kwargs: [])
    runtime.system_prompt = runtime._compose_system_prompt(set())
    runtime._system_prompt_revision = 1
    runtime._workspace_instruction_revision = workspace_instructions.workspace_instruction_fingerprint()

    async def collect(conv_id):
        return [event async for event in runtime.run("Work", conv_id)]

    async def run_both():
        return await asyncio.gather(collect("one"), collect("two"))

    results = asyncio.run(run_both())
    assert len(results) == 2
    assert workspace.selected_workspace_dir() == ""
    assert runtime._active_runs == 0
    assert Path.cwd() != projects[0]


def test_approval_pins_relative_paths_to_original_project(tmp_path, monkeypatch):
    from app.agent import approval_broker

    token = workspace.set_workspace_dir(str(tmp_path))
    try:
        result = file_ops._pending_approval_result(
            SimpleNamespace(reason="Approve", reason_code="confirmation_required", policy_source="test"), tool_name="file_write", action_type="mutate",
            path=str(tmp_path / "notes.txt"), action_description="Write notes",
            payload_args={"path": "notes.txt", "content": "Hello"},
        )
        ticket = approval_broker.get_ticket(json.loads(result)["ticket_id"])
        assert ticket.payload["args"]["path"] == str(tmp_path / "notes.txt")
        assert ticket.payload["args"]["content"] == "Hello"
    finally:
        workspace.reset_workspace_dir(token)


def test_paths_expand_environment_variables_before_resolving_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("MONAW_TEST_PROJECT", str(tmp_path))
    token = workspace.set_workspace_dir(str(tmp_path / "other"))
    try:
        assert workspace.workspace_path("%MONAW_TEST_PROJECT%/notes.txt") == tmp_path / "notes.txt"
        assert workspace.validate_workspace_dir("%MONAW_TEST_PROJECT%") == str(tmp_path.resolve())
    finally:
        workspace.reset_workspace_dir(token)
