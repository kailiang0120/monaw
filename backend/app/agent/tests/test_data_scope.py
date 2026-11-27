import pytest

from app.agent.action_security.data_scope import (
    resolve_upload_scope,
    revalidate_upload_scope,
)
from app.agent.action_security.sensitive_data import redact_sensitive


def test_upload_scope_binds_workspace_content_identity_and_sensitive_files(
    tmp_path, monkeypatch
):
    from app.agent import controller_policy
    from app.agent.settings_store import AgentSettings

    settings = AgentSettings()
    settings.permissions.mode = "full_access"
    settings.permissions.blocked_roots = []
    settings.browser.upload_workspace_roots = [str(tmp_path)]
    monkeypatch.setattr(controller_policy, "_load_runtime_settings", lambda: settings)
    report = tmp_path / "report.txt"
    report.write_text("original")
    scope = resolve_upload_scope([str(report)])
    assert scope["files"][0]["path"] == str(report.resolve())
    assert revalidate_upload_scope(scope) == scope
    report.write_text("replaced")
    with pytest.raises(ValueError, match="upload_scope_changed"):
        revalidate_upload_scope(scope)
    key = tmp_path / ".env"
    key.write_text("TOKEN=private")
    with pytest.raises(ValueError, match="upload_sensitive_file_blocked"):
        resolve_upload_scope([str(key)])
    # A permitted parent directory must not turn recursive content search into
    # a second path to known credentials.
    import json
    from app.skills.filesystem import file_ops

    settings.permissions.mode = "full_access"
    settings.permissions.blocked_roots = []
    assert json.loads(file_ops.file_search(str(tmp_path), "private"))["matches"] == []
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside")
    try:
        with pytest.raises(ValueError, match="upload_outside_workspace"):
            resolve_upload_scope([str(outside)])
    finally:
        outside.unlink()


def test_sensitive_diagnostic_redaction_handles_structured_and_embedded_credentials(
    monkeypatch,
):
    monkeypatch.setenv("APP_API_KEY", "known-private-value")
    payload = {
        "password": "raw",
        "authorization_binding": "safe-digest",
        "error": "known-private-value Bearer abc.def token=abcd -----BEGIN PRIVATE KEY-----\nprivate\n-----END PRIVATE KEY-----",
    }
    safe = redact_sensitive(payload)
    assert safe["authorization_binding"] == "safe-digest"
    for secret in ("known-private-value", "abc.def", "abcd", "\nprivate\n", '"raw"'):
        assert secret not in str(safe)
    assert payload["password"] == "raw"


def test_browser_inputs_omit_opaque_payload_in_stream_and_persisted_ledger():
    from app.agent.harness.tool_batch import ToolBatchLedger
    from app.agent.run_events import event_dict
    import json

    name = "mcp__chrome-devtools__evaluate_script"
    args = {"pageId": 7, "function": "() => fetch('private-unlabelled-payload')"}
    event = event_dict({"event": "tool_start", "data": {"tool": name, "input": args}})
    ledger = ToolBatchLedger()
    ledger.register(call_id="c", tool_name=name, arguments=args, obs_started_at=1)
    assert "private-unlabelled-payload" not in str(event)
    assert "private-unlabelled-payload" not in str(ledger.persistable_calls())
    assert json.loads(ledger.persistable_calls()[0]["input"])["pageId"] == 7
    assert args["function"].endswith("'private-unlabelled-payload')")


def test_logging_redaction_preserves_uvicorn_access_formatter_arguments():
    import logging
    from uvicorn.logging import AccessFormatter
    from app.agent.action_security.sensitive_data import install_logging_redaction

    install_logging_redaction()
    record = logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:9000", "GET", "/resource?token=private-access-value", "1.1", 200),
        None,
    )
    formatter = AccessFormatter(
        fmt='%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False
    )
    formatted = formatter.format(record)
    assert "GET /resource?token=[REDACTED] HTTP/1.1" in formatted
    assert "200 OK" in formatted
    assert "private-access-value" not in formatted
    assert isinstance(record.args, tuple) and record.args[4] == 200


def test_feature_flags_preserve_json_replay_and_review_eligibility(monkeypatch):
    """Feature flag names must not turn benign booleans into redacted credentials."""
    import json
    from google.genai import types
    from app.agent.provider_history import (
        encode_provider_history,
        decode_provider_history,
    )
    from app.agent.approval_broker import ApprovalTicket
    from app.agent.approval_review import review_action

    monkeypatch.setenv("MONAW_TEST_CREDENTIAL_FLAG", "false")
    monkeypatch.setenv("MONAW_SECRET_ENABLED", "true")
    monkeypatch.setenv("TOKEN_LIMIT", "2048")
    monkeypatch.setenv("APP_CLIENT_SECRET", "false")
    result = {"status": "ok", "is_blank_page": False, "limit": 2048}
    wire = json.dumps(result)
    assert redact_sensitive(wire) == wire
    assert json.loads(redact_sensitive(wire)) == result
    history = {
        "provider": "gemini",
        "model_name": "gemini-4-neon",
        "messages": [
            {
                "role": "model",
                "gemini_parts": [
                    types.Part(
                        function_call=types.FunctionCall(
                            id="read-1", name="read", args={"path": "report.txt"}
                        ),
                        thought_signature=b"signed",
                    )
                ],
            },
            {
                "role": "user",
                "gemini_function_response": {
                    "id": "read-1",
                    "name": "read",
                    "response": result,
                },
            },
        ],
    }
    encoded = encode_provider_history(history, [1, 2])
    assert encoded
    assert decode_provider_history(
        encoded, provider="gemini", model_name="gemini-4-neon", owner_id=2
    )
    ticket = ApprovalTicket(
        id="flag-check",
        action_type="exec",
        tool_name="exec",
        description="Inspect",
        payload={
            "args": {"command": "echo false", "sandbox": {"user_review_only": False}}
        },
    )

    class Reviewer:
        async def chat(self, **kwargs):
            """Approve the harmless action to prove it reaches the reviewer."""
            return '{"decision":"approve","reason":"Requested inspection."}'

    import asyncio

    assert (
        asyncio.run(review_action(Reviewer(), ticket, "Inspect the flag"))["decision"]
        == "approve"
    )


def test_registered_credentials_and_json_values_are_redacted_without_changing_types(
    monkeypatch,
):
    """Narrow environment matching still protects real keys, including JSON-escaped values."""
    import json
    from app.security import mcp_credentials

    monkeypatch.setattr(mcp_credentials, "_REDACTIONS", [])
    monkeypatch.setenv("APP_API_KEY", "real-private-environment-key")
    monkeypatch.setenv("GITHUB_TOKEN", "real-private-provider-token")
    monkeypatch.setenv("APP_PASSWORD", "123456")
    secret = 'registered-private-quote"value'
    mcp_credentials.remember_secrets([secret])
    payload = {
        "ok": True,
        "count": 2,
        "result": secret,
        "key_echo": "real-private-environment-key",
        "nested": {"access_token": "another-private-token"},
    }
    original = json.dumps(payload)
    safe = json.loads(redact_sensitive(original))
    assert safe["ok"] is True and safe["count"] == 2
    assert safe["result"] == "[REDACTED]" and safe["key_echo"] == "[REDACTED]"
    assert safe["nested"]["access_token"] == "[REDACTED]"
    assert payload["result"] == secret
    assert redact_sensitive("echo real-private-provider-token") == "echo [REDACTED]"
    assert redact_sensitive("password was 123456") == "password was [REDACTED]"
    mcp_credentials.remember_secrets(["opaque-value-one", "opaque-value-two"])
    safe_keys = json.loads(redact_sensitive(json.dumps({"opaque-value-one": 1, "opaque-value-two": 2})))
    assert len(safe_keys) == 2 and set(safe_keys.values()) == {1, 2}
    assert "opaque-value" not in json.dumps(safe_keys)
