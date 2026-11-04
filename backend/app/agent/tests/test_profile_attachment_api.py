"""HTTP flow coverage for profile-authenticated desktop attachment use."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent.database import get_db
from app.agent.durable.turn_run import create_durable_run
from app.agent.run_context import (
    reset_current_control_session_id,
    reset_current_conversation_id,
    reset_current_execution_source,
    reset_current_principal_id,
    set_current_control_session_id,
    set_current_conversation_id,
    set_current_execution_source,
    set_current_principal_id,
)
from app.agent.response_attachments import (
    collect_response_attachments,
    resolve_attachment_handle_path,
)
from app.main import app
from app.security.control_plane import ALL_SCOPES, mint_control_token


PROFILE_ID = "profile-attachment-owner"
SESSION_ID = "attachment-session-current"
OTHER_SESSION_ID = "attachment-session-restarted"
CONVERSATION_ID = "profile-attachment-chat"
RUN_ID = "run-profile-attachment-flow"


@pytest.fixture
def profile_attachment_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.agent import response_attachments
    from app.api.routes import chat, uploads

    db = get_db()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db.create_conversation(CONVERSATION_ID, "Attachment flow", str(workspace))

    runtime_root = tmp_path / "runtime"

    def temporary_runtime_path(*parts: str) -> Path:
        target = runtime_root.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    monkeypatch.setattr(uploads, "runtime_path", temporary_runtime_path)
    monkeypatch.setattr(
        response_attachments,
        "_ATTACHMENT_REGISTRY_PATH",
        tmp_path / "attachment_registry.json",
    )
    monkeypatch.setattr(response_attachments, "_ATTACHMENT_REGISTRY_LOADED", False)
    monkeypatch.setattr(response_attachments, "_is_read_allowed_by_policy", lambda _path: True)
    response_attachments._ATTACHMENT_REGISTRY.clear()

    observed: dict[str, object] = {}
    generated_path = tmp_path / "workspace" / "generated-report.txt"

    async def fake_run_agent_stream(
        *,
        message,
        conversation_id,
        settings,
        attachments,
        control_session_id,
        execution_source,
        principal_id,
        durable_run_id,
        **_kwargs,
    ):
        assert conversation_id == CONVERSATION_ID
        assert execution_source == "desktop"
        assert principal_id == PROFILE_ID
        assert len(attachments) == 1
        runtime_input = attachments[0]
        assert Path(runtime_input["path"]).read_text(encoding="utf-8") == "user supplied bytes"
        assert runtime_input["handle"] == f"attachment://{observed['uploaded_id']}"
        assert f"attachment://{observed['uploaded_id']}" in message
        observed["runtime_input_path"] = runtime_input["path"]
        observed["runtime_principal_id"] = principal_id

        run = create_durable_run(
            conversation_id=conversation_id,
            execution_source=execution_source,
            principal_id=principal_id,
            control_session_id=control_session_id,
            workspace_dir=str(workspace),
            run_id=durable_run_id,
            turn_id=f"turn-{durable_run_id}",
            db=db,
        )
        generated_path.write_text("generated attachment bytes", encoding="utf-8")
        context_tokens = [
            (reset_current_conversation_id, set_current_conversation_id(conversation_id)),
            (reset_current_control_session_id, set_current_control_session_id(control_session_id)),
            (reset_current_execution_source, set_current_execution_source(execution_source)),
            (reset_current_principal_id, set_current_principal_id(principal_id)),
        ]
        try:
            assert resolve_attachment_handle_path(runtime_input["handle"]) == Path(runtime_input["path"])
            generated = collect_response_attachments(content=f"Created report: {generated_path}")
        finally:
            for reset, token in reversed(context_tokens):
                reset(token)
        assert len(generated) == 1
        observed["generated"] = generated[0]
        db.persist_turn_messages(
            run.turn_id,
            conversation_id,
            [
                {"role": "user", "content": message},
                {"role": "assistant", "content": "The report is ready.", "attachments": generated},
            ],
        )
        assert run.finalize("complete")
        yield {
            "event": "done",
            "data": {
                "conversation_id": conversation_id,
                "summary": "The report is ready.",
                "status": "complete",
                "attachments": generated,
            },
        }

    from app.agent import runtime

    monkeypatch.setattr(runtime, "run_agent_stream", fake_run_agent_stream)
    monkeypatch.setattr(chat, "build_runtime_namespace", lambda *_args: object())
    monkeypatch.setattr(chat, "load_agent_settings", lambda *_args: object())
    return {
        "client": TestClient(app),
        "db": db,
        "workspace": workspace,
        "observed": observed,
        "generated_path": generated_path,
    }


def _headers(session_id: str) -> dict[str, str]:
    import os

    token = mint_control_token(
        os.environ["MONAW_CONTROL_SECRET"],
        scopes=ALL_SCOPES,
        session_id=session_id,
        profile_id=PROFILE_ID,
    )
    return {"Authorization": f"Bearer {token}"}


def _done_event(stream_text: str) -> dict:
    for block in stream_text.split("\n\n"):
        lines = block.splitlines()
        if any(line.strip() == "event: done" for line in lines):
            data = next(line[5:].strip() for line in lines if line.startswith("data:"))
            return json.loads(data)
    raise AssertionError(f"No done event in chat stream: {stream_text!r}")


def test_signed_profile_upload_chat_runtime_and_generated_download_stay_session_scoped(
    profile_attachment_context,
):
    context = profile_attachment_context
    client = context["client"]
    current_headers = _headers(SESSION_ID)
    upload = client.post(
        "/api/uploads",
        headers=current_headers,
        json={
            "filename": "input.txt",
            "data_base64": base64.b64encode(b"user supplied bytes").decode("ascii"),
            "mime_type": "text/plain",
            "conversation_id": CONVERSATION_ID,
        },
    )
    assert upload.status_code == 200, upload.text
    uploaded = upload.json()
    context["observed"]["uploaded_id"] = uploaded["id"]

    # A renewed token for the same signed profile still cannot borrow the old
    # process's upload capability.
    wrong_session_chat = client.post(
        "/api/chat",
        headers=_headers(OTHER_SESSION_ID),
        json={
            "message": "Read this attachment",
            "conversation_id": CONVERSATION_ID,
            "run_id": "run-other-attachment-session",
            "attachments": [uploaded],
        },
    )
    assert wrong_session_chat.status_code == 404
    assert "user supplied bytes" not in wrong_session_chat.text

    chat_response = client.post(
        "/api/chat",
        headers=current_headers,
        json={
            "message": "Read the upload and prepare a report",
            "conversation_id": CONVERSATION_ID,
            "run_id": RUN_ID,
            "attachments": [uploaded],
        },
    )
    assert chat_response.status_code == 200, chat_response.text
    done = _done_event(chat_response.text)
    assert len(done["attachments"]) == 1
    generated = done["attachments"][0]
    assert context["observed"]["runtime_input_path"]
    assert context["observed"]["runtime_principal_id"] == PROFILE_ID

    run = context["db"].fetchone(
        "SELECT owner_principal_id, control_session_id, execution_source FROM agent_runs WHERE run_id = ?",
        (RUN_ID,),
    )
    assert run is not None
    assert run["owner_principal_id"] == PROFILE_ID
    assert run["control_session_id"] == SESSION_ID
    assert run["execution_source"] == "desktop"

    downloaded = client.get(
        f"/api/files/{generated['id']}?conversation_id={CONVERSATION_ID}",
        headers=current_headers,
    )
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == b"generated attachment bytes"

    # Profile identity permits Run recovery across app restarts, while file
    # capabilities continue to require the live control session that created them.
    other_session_download = client.get(
        f"/api/files/{generated['id']}?conversation_id={CONVERSATION_ID}",
        headers=_headers(OTHER_SESSION_ID),
    )
    assert other_session_download.status_code == 404
    assert other_session_download.content != b"generated attachment bytes"
