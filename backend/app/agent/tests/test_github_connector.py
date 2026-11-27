"""Core GitHub boundaries using synthetic tokens and no live repository changes."""

import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.agent.approval_broker import approve_ticket, get_ticket
from app.agent.database import get_db
from app.agent.execution_resume import resume_approved_ticket
from app.agent.settings_store import AgentSettings
from app.agent.tests.test_connectors import TestCipher
from app.agent.tool_registry import ToolRegistry
from app.connectors import github
from app.connectors.github_auth import GitHubSignIn
from app.connectors.models import ConnectorError, Credentials
from app.connectors.providers import CallContext, ProviderRegistry
from app.connectors.service import ConnectorService
from app.connectors.store import AccountStore
from app.connectors.tools import manifest_fingerprint, register_connector_tools
from app.connectors.vault import CredentialVault


@pytest.fixture
def connected(monkeypatch):
    """Build a real service with encrypted test storage and fake GitHub responses."""
    import app.connectors.service as module
    import app.agent.controller_policy as policy

    registry = ProviderRegistry()
    registry.register(github.GitHubProvider())
    service = ConnectorService(AccountStore(get_db()), registry, CredentialVault(TestCipher()))
    service.github_sign_in = GitHubSignIn(service)
    monkeypatch.setattr(module, "_service", service)
    monkeypatch.setattr(policy, "_load_runtime_settings", lambda: AgentSettings())
    calls = []

    def request(path, **kwargs):
        """Record requests and return bounded representative API payloads."""
        calls.append((path, kwargs))
        if path == "/user":
            return {"id": 123, "login": "test-user"}, {}
        if path == "/login/device/code":
            return {
                "device_code": "private-device-code",
                "user_code": "ABCD-EFGH",
                "interval": 5,
                "expires_in": 900,
            }, {}
        if path == "/login/oauth/access_token":
            return {
                "access_token": "private-github-access",
                "refresh_token": "private-github-refresh",
                "expires_in": 28800,
            }, {}
        if path == "/user/repos":
            return [{"full_name": "test/repo", "html_url": "https://github.com/test/repo"}], {
                "Link": '<https://evil.invalid>; rel="next"'
            }
        return {"number": 7, "title": "Example", "html_url": "https://github.com/test/repo/issues/7"}, {}

    monkeypatch.setattr(github, "request_json", request)
    return service, calls


def test_device_login_owner_interval_cancel_and_read_default(connected):
    """A login is session bound and credentials never enter its public response."""
    service, calls = connected
    auth = service.github_sign_in
    login = auth.start("desktop")
    assert "private-device-code" not in json.dumps(login)
    with pytest.raises(ConnectorError, match="authorization_invalid"):
        auth.poll(login["login_id"], "other")
    assert auth.poll(login["login_id"], "desktop")["status"] == "pending"
    assert len(calls) == 1
    auth._pending[login["login_id"]].next_poll = 0
    result = auth.poll(login["login_id"], "desktop")
    assert result["status"] == "connected"
    assert "private-github" not in json.dumps(result)
    assert "create_issue" not in result["account"]["enabled_capabilities"]
    account = service.store.get(result["account"]["id"])
    credentials = service.vault.open(account, service.store.credentials(account))
    assert credentials.client_id == github.github_configuration()["client_id"]
    with pytest.raises(ConnectorError, match="authorization_invalid"):
        auth.poll(login["login_id"], "desktop")
    cancelled = auth.start("desktop")
    auth.cancel(cancelled["login_id"], "desktop")
    assert not auth._pending


def test_slow_down_expiry_and_malformed_response(connected, monkeypatch):
    """Honor server backoff and discard expired or invalid exchanges."""
    service, _ = connected
    auth = service.github_sign_in
    login = auth.start("desktop")
    attempt = auth._pending[login["login_id"]]
    attempt.next_poll = 0
    monkeypatch.setattr(github, "request_json", lambda *args, **kwargs: ({"error": "slow_down"}, {}))
    assert auth.poll(login["login_id"], "desktop")["interval"] == 10
    attempt.expires_at = 0
    with pytest.raises(ConnectorError, match="authorization_expired"):
        auth.poll(login["login_id"], "desktop")
    monkeypatch.setattr(
        github,
        "request_json",
        lambda *args, **kwargs: ({"device_code": "test", "user_code": "test", "expires_in": "nan"}, {}),
    )
    with pytest.raises(ConnectorError, match="authorization_invalid"):
        auth.start("desktop")


def test_cancel_during_exchange_never_persists_tokens(connected, monkeypatch):
    """Cancellation stays responsive while GitHub is processing an exchange."""
    service, _ = connected
    auth = service.github_sign_in
    login = auth.start("desktop")
    auth._pending[login["login_id"]].next_poll = 0
    original = github.request_json
    entered, release = threading.Event(), threading.Event()

    def delayed(path, **kwargs):
        """Hold the remote exchange until the local attempt has been cancelled."""
        if path == "/login/oauth/access_token":
            entered.set()
            assert release.wait(5)
        return original(path, **kwargs)

    monkeypatch.setattr(github, "request_json", delayed)
    with ThreadPoolExecutor() as pool:
        result = pool.submit(auth.poll, login["login_id"], "desktop")
        assert entered.wait(5)
        try:
            auth.cancel(login["login_id"], "desktop")
        finally:
            release.set()
        with pytest.raises(ConnectorError, match="authorization_invalid"):
            result.result()
    assert service.store.list() == []


def test_pagination_refresh_and_reconnect_identity(connected, monkeypatch):
    """Read pages remain bounded and refresh uses the original registration ID."""
    service, calls = connected
    public = service.github_sign_in.connect_token("private-test-pat")
    account = service.store.get(public["id"])
    adapter = service.providers.get("github")
    result = service.invoke(
        "github",
        "list_repositories",
        account.id,
        {},
        expected_fingerprint=manifest_fingerprint(adapter.manifest),
    )
    assert result["next_cursor"] == "2"
    assert calls[-1][0] == "/user/repos"
    adapter.refresh(
        Credentials(access_token="old-private", refresh_token="refresh-private", client_id="original-client"),
        CallContext(account, time.monotonic() + 30),
    )
    assert calls[-1][1]["data"]["client_id"] == "original-client"
    assert "client_secret" not in calls[-1][1]["data"]
    monkeypatch.setattr(
        github, "request_json", lambda *args, **kwargs: ({"id": 456, "login": "different-user"}, {})
    )
    with pytest.raises(ConnectorError, match="account_changed"):
        service.github_sign_in.connect_token(
            "different-private", account_id=account.id, revision=account.revision
        )


def test_write_preview_and_exactly_once_approval(connected):
    """No remote write occurs before explicit approval or twice after replay."""
    service, calls = connected
    account = service.github_sign_in.connect_token("private-test-pat", allow_writes=True)
    registry = ToolRegistry()
    register_connector_tools(registry, service)
    invoke = registry.get_tool("connector__github__create_issue")["callable"]
    pending = json.loads(
        invoke(
            account["id"],
            {"repository": "test/repo", "title": "Review this", "body": "Exact proposed content"},
        )
    )
    assert pending["status"] == "pending_approval"
    assert pending["action_descriptor"]["connector_preview"] == {
        "account": "test-user",
        "repository": "test/repo",
        "title": "Review this",
        "body": "Exact proposed content",
    }
    assert len(calls) == 1
    ticket = get_ticket(pending["ticket_id"])
    approve_ticket(ticket.id)
    resume_approved_ticket(ticket)
    resume_approved_ticket(ticket)
    assert ticket.status.value == "applied"
    assert len(calls) == 2
    assert calls[-1][1]["data"]["body"] == "Exact proposed content"


def test_management_routes_and_credential_scope(connected):
    """Ordinary UI sessions may start sign-in but cannot submit raw credentials."""
    from app.main import app

    client = TestClient(app)
    assert client.get("/api/connectors/github/configuration").json()["device_login_available"]
    assert client.post("/api/connectors/github/token", json={"token": "private-test-pat"}).status_code == 403
    response = client.post("/api/connectors/github/login", json={})
    assert response.status_code == 200
    assert "device_code" not in response.json()
    assert client.delete(f"/api/connectors/github/login/{response.json()['login_id']}").status_code == 200


def test_transport_rejects_foreign_origins_and_oauth_paths():
    """No token-bearing request may use an arbitrary destination."""
    for path in ("https://evil.invalid", "//evil.invalid", "/user?token=secret"):
        with pytest.raises(ConnectorError, match="invalid_arguments"):
            github.request_json(path, token="private-test-pat")
    with pytest.raises(ConnectorError, match="invalid_arguments"):
        github.request_json("/user", oauth=True)
