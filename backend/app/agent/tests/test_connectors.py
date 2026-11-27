"""Core connector boundaries using disposable databases and a synthetic provider."""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.agent.approval_broker import approve_ticket, get_ticket
from app.agent.database import get_db
from app.agent.execution_resume import resume_approved_ticket
from app.agent.settings_store import AgentSettings
from app.agent.tool_registry import ToolRegistry
from app.connectors.auth import AuthorizationSessions
from app.connectors.models import Capability, ConnectorError, ConnectorResult, Credentials, Identity, ProviderManifest, RefreshResult
from app.connectors.providers import ProviderRegistry
from app.connectors.service import ConnectorService
from app.connectors.store import AccountStore
from app.connectors.tools import manifest_fingerprint, register_connector_tools
from app.connectors.vault import CredentialVault
from app.security.control_plane import mint_control_token


class TestCipher:
    __test__ = False

    def __init__(self):
        """Keep test-only plaintext in memory; persisted values are random handles."""
        self.values = {}

    def encrypt(self, data, binding):
        """Model authenticated ciphertext without an OS dependency in Linux CI."""
        handle = os.urandom(32)
        self.values[(handle, binding)] = data
        return handle

    def decrypt(self, data, binding):
        """Reject a ciphertext copied to another identity binding."""
        return self.values[(data, binding)]


class ExampleProvider:
    manifest = ProviderManifest(
        id="example", name="Example", version="1", auth_methods={"oauth_pkce", "api_key"},
        capabilities=(
            Capability(id="search", description="Search messages", action="read", required_scopes={"read"},
                       parameters={"type": "object", "properties": {"query": {"type": "string"}},
                                   "required": ["query"], "additionalProperties": False}),
            Capability(id="send", description="Send a message", action="send_message", required_scopes={"send"},
                       parameters={"type": "object", "properties": {"body": {"type": "string"}},
                                   "required": ["body"], "additionalProperties": False}),
        ),
    )

    def __init__(self):
        """Record actual adapter invocations to detect unintended execution."""
        self.calls = []
        self.refreshes = 0
        self.failure = None

    def refresh(self, credentials, context):
        """Rotate synthetic credentials without any external request."""
        context.remaining()
        self.refreshes += 1
        return RefreshResult(credentials=Credentials(access_token="new-private-access", refresh_token="new-private-refresh",
                             expires_at=time.time() + 3600))

    def invoke(self, capability, arguments, credentials, context):
        """Return a page bound to the selected account, including an intentional secret echo."""
        context.remaining()
        self.calls.append((context.account.id, capability.id, arguments))
        if self.failure:
            raise self.failure
        return ConnectorResult(data={"subject": context.account.subject,
                                    "echo": credentials.access_token.get_secret_value()}, next_cursor="page2")


@pytest.fixture
def foundation(monkeypatch):
    """Use the test-isolated Monaw DB and ordinary action policy."""
    import app.connectors.service as module
    import app.agent.controller_policy as policy

    registry = ProviderRegistry()
    adapter = ExampleProvider()
    registry.register(adapter)
    service = ConnectorService(AccountStore(get_db()), registry, CredentialVault(TestCipher()))
    monkeypatch.setattr(module, "_service", service)
    monkeypatch.setattr(policy, "_load_runtime_settings", lambda: AgentSettings())
    yield service, adapter


def connect(service, *, subject="alice", expires_at=None, capabilities=frozenset({"search", "send"})):
    """Complete a synthetic provider-verified authentication result."""
    return service.connect_verified("example", Identity(subject=subject, label=subject,
        granted_scopes={"read", "send"}), Credentials(access_token="private-access-" + subject + '\\"',
        refresh_token="private-refresh-" + subject, expires_at=expires_at),
        auth_method="oauth_pkce", enabled_capabilities=capabilities)


def invoke(service, adapter, account, capability="search", arguments=None, **kwargs):
    """Exercise the same policy-aware service that tool callbacks call."""
    return service.invoke("example", capability, account.id, arguments or {"query": "hello"},
                          expected_fingerprint=manifest_fingerprint(adapter.manifest), **kwargs)


def test_multiple_accounts_persistence_and_ciphertext_binding(foundation):
    service, adapter = foundation
    alice, bob = connect(service), connect(service, subject="bob")
    assert alice.id != bob.id
    result = invoke(service, adapter, bob)
    assert result["data"]["subject"] == "bob"
    assert result["data"]["echo"] == "[REDACTED]"
    assert result["next_cursor"] == "page2"
    assert "private-access" not in repr(service.vault.open(alice, service.store.credentials(alice)))
    raw = service.store.db._path.read_bytes()
    assert b"private-access" not in raw and b"private-refresh" not in raw
    restored = ConnectorService(AccountStore(service.store.db), service.providers, service.vault)
    assert restored.store.get(alice.id) == alice
    with pytest.raises(ConnectorError, match="credentials_unavailable"):
        service.vault.open(bob, service.store.credentials(alice))
    with pytest.raises(ConnectorError, match="account_exists"):
        connect(service)


def test_permissions_revision_and_disconnect_block_stale_tools(foundation):
    service, adapter = foundation
    account = connect(service)
    tools = ToolRegistry()
    register_connector_tools(tools, service)
    call = tools.get_tool("connector__example__search")["callable"]
    fingerprint = service.fingerprint()
    updated = service.update(account.id, expected_revision=1, enabled=True,
                             enabled_capabilities=frozenset({"search"}), label="Work")
    assert updated.id == account.id and service.fingerprint() != fingerprint
    with pytest.raises(ConnectorError, match="capability_disabled"):
        invoke(service, adapter, updated, "send", {"body": "hello"})
    with pytest.raises(ConnectorError, match="account_changed"):
        service.disconnect(account.id, expected_revision=1)
    disconnected = service.disconnect(account.id, expected_revision=updated.revision)
    assert disconnected.state == "disconnected"
    assert json.loads(call(account.id, {"query": "hi"}))["code"] == "account_disabled"
    assert service.store.db.fetchone("SELECT credential_blob FROM connector_accounts WHERE id = ?",
                                     (account.id,))[0] is None
    with pytest.raises(ConnectorError, match="account_changed"):
        service.connect_verified("example", Identity(subject="mallory", label="Other"),
            Credentials(access_token="unrelated"), auth_method="oauth_pkce", enabled_capabilities=frozenset(),
            account_id=account.id, expected_revision=disconnected.revision)
    assert adapter.calls == []


def test_scopes_schema_and_adapter_version_are_enforced(foundation):
    service, adapter = foundation
    with pytest.raises(ConnectorError, match="scope_missing"):
        service.connect_verified("example", Identity(subject="limited", label="Limited", granted_scopes={"read"}),
            Credentials(access_token="private-limited"), auth_method="oauth_pkce",
            enabled_capabilities=frozenset({"send"}))
    account = connect(service)
    with pytest.raises(ConnectorError, match="invalid_arguments"):
        invoke(service, adapter, account, arguments={"query": 9})
    with pytest.raises(ConnectorError, match="account_changed"):
        service.invoke("example", "search", account.id, {"query": "hi"}, expected_fingerprint="old-schema")
    assert adapter.calls == []


def test_concurrent_expiry_refreshes_once_and_saves_rotated_credentials(foundation):
    service, adapter = foundation
    account = connect(service, expires_at=time.time() - 1)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: invoke(service, adapter, account), range(4)))
    assert all(result["status"] == "ok" for result in results)
    assert adapter.refreshes == 1
    current = service.store.get(account.id)
    assert current.state == "ready"
    saved = service.vault.open(current, service.store.credentials(current))
    assert saved.refresh_token.get_secret_value() == "new-private-refresh"


def test_reduced_grants_and_revocation_disable_tools(foundation, monkeypatch):
    service, adapter = foundation
    account = connect(service, expires_at=time.time() - 1)

    def reduced(credentials, context):
        """Model an OAuth refresh that drops a previously granted scope."""
        return RefreshResult(credentials=Credentials(access_token="reduced-private", expires_at=time.time() + 3600),
                             granted_scopes=frozenset())

    monkeypatch.setattr(adapter, "refresh", reduced)
    with pytest.raises(ConnectorError, match="scope_missing"):
        invoke(service, adapter, account)
    assert not service.store.get(account.id).enabled_capabilities
    assert not adapter.calls
    bob = connect(service, subject="bob")
    adapter.failure = ConnectorError("needs_reauth")
    with pytest.raises(ConnectorError, match="needs_reauth"):
        invoke(service, adapter, bob)
    assert service.store.get(bob.id).state == "needs_reauth"


def test_refresh_interruption_and_invalid_grant_require_reconnect(foundation, monkeypatch):
    service, adapter = foundation
    account = connect(service, expires_at=time.time() - 1)

    def interrupted(credentials, context):
        """Simulate token rotation followed by a lost network response."""
        raise TimeoutError("provider body contains private-refresh-alice")

    monkeypatch.setattr(adapter, "refresh", interrupted)
    with pytest.raises(ConnectorError, match="needs_reauth"):
        invoke(service, adapter, account)
    assert service.store.get(account.id).state == "needs_reauth"
    second = connect(service, subject="bob")
    service.store.replace(second.model_copy(update={"state": "refreshing", "revision": 2}), 1)
    restarted = ConnectorService(service.store, service.providers, service.vault)
    with pytest.raises(ConnectorError, match="needs_reauth"):
        invoke(restarted, adapter, second)
    assert adapter.calls == []


def test_write_approval_replays_once_and_rejects_changed_account(foundation):
    service, adapter = foundation
    account = connect(service)
    registry = ToolRegistry()
    register_connector_tools(registry, service)
    call = registry.get_tool("connector__example__send")["callable"]
    pending = json.loads(call(account.id, {"body": "private message"}))
    assert pending["status"] == "pending_approval"
    assert adapter.calls == []
    ticket = get_ticket(pending["ticket_id"])
    approve_ticket(ticket.id)
    resume_approved_ticket(ticket)
    resume_approved_ticket(ticket)
    assert ticket.status.value == "applied"
    assert len(adapter.calls) == 1
    pending = json.loads(call(account.id, {"body": "another message"}))
    ticket = get_ticket(pending["ticket_id"])
    service.disconnect(account.id, expected_revision=service.store.get(account.id).revision)
    approve_ticket(ticket.id)
    resume_approved_ticket(ticket)
    assert ticket.status.value == "failed" and len(adapter.calls) == 1


def test_background_writes_use_existing_policy_and_failed_writes_are_not_retried(foundation, monkeypatch):
    from app.agent.run_context import set_current_interactive, reset_current_interactive

    service, adapter = foundation
    account = connect(service)
    token = set_current_interactive(False)
    try:
        result = invoke(service, adapter, account, "send", {"body": "hello"})
        assert result["status"] == "denied"
    finally:
        reset_current_interactive(token)
    monkeypatch.setattr("app.agent.action_security.authorize_action", lambda *args, **kwargs: None)
    adapter.failure = TimeoutError("private-provider-body")
    with pytest.raises(ConnectorError) as failure:
        invoke(service, adapter, account, "send", {"body": "hello"})
    assert failure.value.uncertain and len(adapter.calls) == 1
    assert "private" not in json.dumps(failure.value.payload())


def test_authorization_owner_provider_expiry_and_replay():
    now = [100.0]
    sessions = AuthorizationSessions(clock=lambda: now[0])
    fields = dict(provider_id="example", owner_session="desktop", issuer="https://issuer.example",
                  redirect_uri="http://127.0.0.1:4567/callback")
    attempt = sessions.begin(**fields, scopes=frozenset({"read"}))
    assert len(attempt.challenge) == 43 and attempt.verifier not in repr(attempt)
    with pytest.raises(ConnectorError, match="authorization_invalid"):
        sessions.consume(attempt.state, **{**fields, "owner_session": "other"})
    assert sessions.consume(attempt.state, **fields) == attempt
    with pytest.raises(ConnectorError, match="authorization_invalid"):
        sessions.consume(attempt.state, **fields)
    attempt = sessions.begin(**fields, scopes=frozenset())
    now[0] += 601
    with pytest.raises(ConnectorError, match="authorization_expired"):
        sessions.consume(attempt.state, **fields)


def test_api_permissions_and_public_projection(foundation):
    from app.main import app

    service, _ = foundation
    account = connect(service)
    client = TestClient(app)
    secret = os.environ["MONAW_CONTROL_SECRET"]
    reader = {"Authorization": "Bearer " + mint_control_token(secret, scopes=["settings:read"])}
    writer = {"Authorization": "Bearer " + mint_control_token(secret, scopes=["settings:write"])}
    assert client.get("/api/connectors/accounts", headers={"Authorization": ""}).status_code == 401
    response = client.get("/api/connectors/accounts", headers=reader)
    assert response.status_code == 200
    assert "private-access" not in response.text and "credential_blob" not in response.text
    assert client.get("/api/connectors/providers", headers=reader).json()["providers"][0]["id"] == "example"
    endpoint = f"/api/connectors/accounts/{account.id}/disconnect"
    assert client.post(endpoint, json={"expected_revision": 1}, headers=reader).status_code == 403
    assert client.post(endpoint, json={"expected_revision": 1}, headers=writer).status_code == 200
    assert client.post(endpoint, json={"expected_revision": 1}, headers=writer).status_code == 409
    removed = client.request("DELETE", f"/api/connectors/accounts/{account.id}",
                             json={"expected_revision": 2}, headers=writer)
    assert removed.status_code == 200 and service.store.list() == []


def test_discovery_classification_and_cancellation_use_shared_runtime(foundation):
    from app.agent.durable.operation_policy import classify_tool
    from app.agent.skill_loader import load_tools
    from app.agent.tool_cancellation import cancel_call, set_current_call_id, reset_current_call_id, finish_cancellation

    service, adapter = foundation
    account = connect(service)
    _, registry = load_tools(AgentSettings())
    assert registry.get_tool("connector__example__search") is not None
    assert classify_tool("connector__example__search", trusted_builtin=True).effect_class == "read_only"
    assert classify_tool("connector__example__send", trusted_builtin=True).effect_class == "opaque_effect"
    assert classify_tool("connector__example__search").effect_class == "opaque_effect"
    assert classify_tool("connector__fake__search", trusted_builtin=True).effect_class == "opaque_effect"
    token = set_current_call_id("cancel-connector")
    cancel_call("cancel-connector")
    try:
        with pytest.raises(ConnectorError, match="cancelled"):
            invoke(service, adapter, account)
    finally:
        finish_cancellation("cancel-connector")
        reset_current_call_id(token)
    assert not adapter.calls


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-specific")
def test_real_windows_encryption_survives_reopen_and_rejects_rebinding(foundation):
    service, _ = foundation
    account = connect(service)
    vault = CredentialVault()
    credentials = Credentials(access_token="windows-access", refresh_token="windows-refresh")
    encrypted = vault.seal(account, credentials)
    assert b"windows-access" not in encrypted
    assert CredentialVault().open(account, encrypted) == credentials
    with pytest.raises(ConnectorError, match="credentials_unavailable"):
        vault.open(account.model_copy(update={"subject": "different"}), encrypted)
