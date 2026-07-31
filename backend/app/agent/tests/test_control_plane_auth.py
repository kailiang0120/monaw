import os
import re
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.main import app
from app.security.request_limits import RequestSizeLimitMiddleware
from app.security.control_plane import mint_control_token


def test_health_is_the_only_unauthenticated_runtime_endpoint():
    client = TestClient(app)

    health = client.get("/health")
    protected = client.get("/api/settings", headers={"Authorization": ""})
    docs = client.get("/docs")
    openapi = client.get("/openapi.json")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert protected.status_code == 401
    assert protected.json()["code"] == "unauthorized"
    assert protected.headers["www-authenticate"] == "Bearer"
    assert docs.status_code == 404
    assert openapi.status_code == 404


def test_expired_control_session_is_rejected():
    secret = os.environ["MONAW_CONTROL_SECRET"]
    token = mint_control_token(secret, now=100, lifetime_seconds=1)
    client = TestClient(app)

    response = client.get(
        "/api/settings",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401


def test_tampered_control_session_is_rejected():
    secret = os.environ["MONAW_CONTROL_SECRET"]
    token = mint_control_token(secret)
    encoded, signature = token.split(".", 1)
    tampered = f"{encoded[:-1]}A.{signature}"
    client = TestClient(app)

    response = client.get(
        "/api/settings",
        headers={"Authorization": f"Bearer {tampered}"},
    )

    assert response.status_code == 401


def test_route_scope_is_enforced():
    secret = os.environ["MONAW_CONTROL_SECRET"]
    token = mint_control_token(secret, scopes={"settings:read"})
    client = TestClient(app)

    read_response = client.get(
        "/api/settings",
        headers={"Authorization": f"Bearer {token}"},
    )
    write_response = client.put(
        "/api/settings",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )

    assert read_response.status_code == 200
    assert write_response.status_code == 403
    assert write_response.json()["code"] == "forbidden"
    assert write_response.json()["message"] == "Missing required scope: settings:write"
    assert write_response.json()["request_id"]


def test_request_size_limit_rejects_before_route_processing():
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        headers={"Content-Length": str(4 * 1024 * 1024 + 1)},
        content=b"",
    )

    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"
    assert response.json()["message"] == "Request body is too large"
    assert response.json()["request_id"]
    assert response.headers["x-request-id"]


def test_streamed_request_size_limit_cannot_be_bypassed_without_content_length():
    client = TestClient(app)

    def chunks():
        for _ in range(5):
            yield b"x" * (1024 * 1024)

    response = client.post("/api/chat", content=chunks())

    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"
    assert response.json()["message"] == "Request body is too large"


def test_request_size_limit_does_not_disconnect_streaming_response():
    stream_app = FastAPI()
    stream_app.add_middleware(RequestSizeLimitMiddleware)

    @stream_app.post("/api/chat-test")
    async def chat_test():
        async def stream():
            yield b"ok"

        return StreamingResponse(stream(), media_type="text/plain")

    client = TestClient(stream_app)

    response = client.post("/api/chat-test", json={"message": "hi"})

    assert response.status_code == 200
    assert response.text == "ok"


def test_strict_controller_policy_update_rejects_unknown_fields_with_error_envelope():
    client = TestClient(app)

    response = client.put(
        "/api/settings/controller-policy",
        json={"mode": "custom", "unexpected": True},
    )

    body = response.json()
    assert response.status_code == 422
    assert body["code"] == "validation_error"
    assert body["message"] == "Request validation failed"
    assert body["request_id"]
    assert body["details"]["errors"][0]["loc"] == ["body", "unexpected"]
    assert body["details"]["errors"][0]["type"] == "extra_forbidden"


def test_strict_settings_update_rejects_unknown_fields_with_error_envelope():
    client = TestClient(app)

    response = client.put(
        "/api/settings",
        json={"identity": {"user_name": "Kai"}, "unexpected": True},
    )

    body = response.json()
    assert response.status_code == 422
    assert body["code"] == "validation_error"
    assert body["message"] == "Request validation failed"
    assert body["details"]["errors"][0]["loc"] == ["body", "unexpected"]
    assert body["details"]["errors"][0]["type"] == "extra_forbidden"


def test_settings_update_rejects_stale_expected_version():
    client = TestClient(app)

    response = client.put(
        "/api/settings",
        json={
            "expected_settings_version": "stale-version",
            "identity": {"user_name": "Kai"},
        },
    )

    body = response.json()
    assert response.status_code == 409
    assert body["code"] == "settings_version_conflict"
    assert body["message"] == "Settings were changed by another session. Reload settings and try again."
    assert body["details"]["current_settings_version"]


def test_api_pagination_limits_cannot_be_bypassed():
    client = TestClient(app)

    urls = [
        "/api/conversations?limit=201",
        "/api/conversations/conv-test/messages?limit=201",
        "/api/messages/1/tool-calls?limit=201",
        "/api/messages/1/tool-calls?tool_payload_limit=20001",
        "/api/approvals/pending?limit=101",
        "/api/approvals/history?limit=201",
        "/api/access-grants/pending?limit=101",
    ]

    for url in urls:
        response = client.get(url)
        body = response.json()
        assert response.status_code == 422, url
        assert body["code"] == "validation_error"
        assert body["message"] == "Request validation failed"
        assert body["request_id"]


def test_control_plane_fails_closed_without_secret(monkeypatch):
    monkeypatch.delenv("MONAW_CONTROL_SECRET", raising=False)
    client = TestClient(app)

    response = client.get("/api/settings", headers={"Authorization": "Bearer anything"})

    assert response.status_code == 503


def test_cors_allows_exact_desktop_origin_only(monkeypatch):
    client = TestClient(app)
    allowed = client.options(
        "/api/settings",
        headers={
            "Origin": "null",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    rejected = client.options(
        "/api/settings",
        headers={
            "Origin": "http://evil.local",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "null"
    assert rejected.status_code == 400


def test_json_api_routes_declare_response_models():
    streaming_or_file_routes = {
        ("POST", "/api/chat"),
        ("GET", "/api/chat/jobs/{job_id}/stream"),
        ("GET", "/api/events"),
        ("GET", "/api/files/{attachment_id}"),
        ("GET", "/api/files/{attachment_id}/preview"),
    }
    missing: list[str] = []

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not route.path.startswith("/api"):
            continue
        if route.status_code == 204:
            continue
        for method in sorted(route.methods or set()):
            if method in {"HEAD", "OPTIONS"}:
                continue
            if (method, route.path) in streaming_or_file_routes:
                continue
            if route.response_model is None:
                missing.append(f"{method} {route.path}")

    assert missing == []


def _frontend_interface_fields(interface_name: str) -> set[str]:
    repo_root = Path(__file__).resolve().parents[4]
    source = (repo_root / "frontend" / "src" / "lib" / "api" / "types.ts").read_text(encoding="utf-8")
    match = re.search(rf"export interface {re.escape(interface_name)}\s*\{{", source)
    assert match is not None, f"Missing frontend API interface {interface_name}"

    depth = 1
    index = match.end()
    while index < len(source) and depth > 0:
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
        index += 1
    body = source[match.end(): index - 1]

    fields: set[str] = set()
    nested_depth = 0
    for line in body.splitlines():
        stripped = line.strip()
        if nested_depth == 0:
            field_match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\??\s*:", stripped)
            if field_match:
                fields.add(field_match.group(1))
        nested_depth += stripped.count("{") - stripped.count("}")
    return fields


def test_frontend_api_types_cover_backend_response_schema_fields():
    schema_to_frontend_interface = {
        "OkResponse": "OkResponse",
        "ChatJobCreateResponse": "ChatJobCreateResponse",
        "ChatJobOut": "ChatJob",
        "AgentSettingsPayload": "AgentSettings",
        "ModelOptionsPayload": "ModelOptions",
        "WorkspaceInstructionsPayload": "WorkspaceInstructions",
        "SpeechToTextStatusPayload": "SpeechToTextStatus",
        "SandboxStatusPayload": "SandboxStatus",
        "DiagnosticsSummaryPayload": "DiagnosticsSummary",
        "MCPServerDiagnosticsOut": "MCPServerDiagnostics",
        "ObservabilitySummaryOut": "ObservabilitySummary",
    }
    schemas = app.openapi()["components"]["schemas"]
    missing: list[str] = []

    for schema_name, interface_name in schema_to_frontend_interface.items():
        properties = set(schemas[schema_name].get("properties", {}))
        frontend_fields = _frontend_interface_fields(interface_name)
        for field in sorted(properties - frontend_fields):
            missing.append(f"{schema_name} -> {interface_name}.{field}")

    assert missing == []


def test_generated_api_docs_are_current():
    repo_root = Path(__file__).resolve().parents[4]
    result = subprocess.run(
        [sys.executable, "scripts/generate-api-docs.py", "--check"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_windows_launcher_enforces_loopback_and_runtime_checks():
    repo_root = Path(__file__).resolve().parents[4]
    source = (repo_root / "scripts" / "start-windows.ps1").read_text(encoding="utf-8")

    assert "function Assert-SafeBackendHost" in source
    assert "function Assert-RuntimeDirectory" in source
    assert "MONAW_ALLOW_UNSAFE_BACKEND_HOST" in source
    assert "Runtime directory must not be a symlink or junction" in source
