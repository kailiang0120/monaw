import os

from fastapi.testclient import TestClient

from app.main import app
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
    assert write_response.json()["detail"] == "Missing required scope: settings:write"


def test_request_size_limit_rejects_before_route_processing():
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        headers={"Content-Length": str(4 * 1024 * 1024 + 1)},
        content=b"",
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "Request body is too large"
    assert response.headers["x-request-id"]


def test_streamed_request_size_limit_cannot_be_bypassed_without_content_length():
    client = TestClient(app)

    def chunks():
        for _ in range(5):
            yield b"x" * (1024 * 1024)

    response = client.post("/api/chat", content=chunks())

    assert response.status_code == 413
    assert response.json()["detail"] == "Request body is too large"


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
