"""Core user-owned Composio setup, isolation, and credential lifecycle checks."""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent.database import get_db
from app.agent.tests.test_connectors import TestCipher
from app.connectors import service as module
from app.connectors.composio import ComposioAccounts, ComposioProvider
from app.connectors.composio_catalog import MANIFESTS
from app.connectors.composio_gateway import ComposioGateway, login_url, tool_arguments
from app.connectors.models import ConnectorError
from app.connectors.providers import CallContext, ProviderRegistry
from app.connectors.service import ConnectorService
from app.connectors.store import AccountStore
from app.connectors.vault import CredentialVault
from app.security.control_plane import mint_control_token


class Gateway:
    def __init__(self):
        """Record remote operations without contacting a real project or mailbox."""
        self.calls = []
        self.status = "ACTIVE"
        self.subject = "alice@example.test"
        self.hook = lambda: None

    def validate(self, key):
        """Reject a synthetic bad key before a working key can be replaced."""
        if key == "invalid-key":
            raise ConnectorError("needs_reauth")

    def auth_config(self, key, provider):
        """Track automated configuration creation so repeated setup cannot duplicate it."""
        self.calls.append(("auth", provider))
        return "ac_test"

    def session(
        self, key, user, provider, connection=None, auth_config=None, remaining=None
    ):
        """Record explicit account pinning and the application's generated user identity."""
        self.calls.append(("session", user, provider, connection, auth_config))
        return "ts_test"

    def link(self, key, session, provider):
        """Return a hosted browser link and its exact future account identifier."""
        return "ca_test", "https://connect.composio.dev/link/test"

    def account(self, key, user, provider, connection, remaining=None):
        """Simulate an account already filtered by owner, toolkit, and connection ID."""
        self.calls.append(("account", user, provider, connection))
        return {"status": self.status, "auth_config": "ac_test"}

    def execute(self, key, session, provider, capability, arguments, remaining=None):
        """Return a verified identity or a bounded mail page, with a cancellation hook."""
        self.hook()
        self.calls.append(("execute", capability, arguments))
        if capability == "get_profile":
            return (
                {"emailAddress": self.subject}
                if provider == "gmail"
                else {"id": self.subject, "mail": self.subject}
            )
        return {"messages": [{"id": "m1"}], "nextPageToken": "page2"}

    def delete_session(self, key, session):
        """Record cleanup without removing the provider connection."""
        self.calls.append(("delete", session))


@pytest.fixture
def composio(monkeypatch):
    """Use the real account service and migrations with an in-memory cipher and gateway."""
    gateway = Gateway()
    service = ConnectorService(
        AccountStore(get_db()), ProviderRegistry(), CredentialVault(TestCipher())
    )
    service.composio = ComposioAccounts(service, gateway)
    for manifest in MANIFESTS:
        # The stub gateway models successful OAuth independently of managed-app availability.
        service.providers.register(
            ComposioProvider(
                manifest.model_copy(update={"setup_notice": ""}), service.composio
            )
        )
    monkeypatch.setattr(module, "_service", service)
    return service, service.composio, gateway


def complete(accounts, provider="gmail"):
    """Complete one paced browser flow without wall-clock sleeps."""
    started = accounts.start("owner", provider)
    accounts.pending[started["login_id"]].next_poll = 0
    assert accounts.poll(started["login_id"], "owner") == {"status": "connected"}
    return accounts.service.store.list()[0]


def test_encrypted_setup_preserves_working_key_and_reuses_auth_config(composio):
    service, accounts, gateway = composio
    accounts.save_key("private-project-key")
    first = accounts.current()
    row = get_db().fetchone("SELECT * FROM composio_configuration")
    assert b"private-project-key" not in bytes(row["key_blob"])
    assert accounts.configuration() == {"configured": True}
    with pytest.raises(ConnectorError):
        accounts.save_key("invalid-key")
    accounts.save_key("private-project-key")
    assert accounts.current() == first
    first_login = accounts.start("owner", "gmail")
    accounts.cancel(first_login["login_id"], "owner")
    account = complete(accounts)
    assert gateway.calls.count(("auth", "gmail")) == 1
    assert account.subject == "alice@example.test"
    saved = service.vault.open(account, service.store.credentials(account))
    assert saved.access_token.get_secret_value() != first.key
    assert saved.gateway.connection_id == "ca_test"
    assert saved.gateway.auth_config_id == "ac_test"


def test_owner_pacing_cancellation_and_key_removal(composio):
    service, accounts, gateway = composio
    accounts.save_key("private-project-key")
    started = accounts.start("owner", "outlook")
    identifier = started["login_id"]
    with pytest.raises(ConnectorError):
        accounts.poll(identifier, "other")
    assert accounts.poll(identifier, "owner")["status"] == "pending"
    accounts.cancel(identifier, "other")
    assert identifier in accounts.pending
    accounts.pending[identifier].next_poll = 0
    assert accounts.poll(identifier, "owner")["status"] == "connected"
    account = service.store.list()[0]
    accounts.remove_key()
    assert accounts.configuration() == {"configured": False}
    assert service.store.get(account.id).state == "disconnected"
    with pytest.raises(ConnectorError):
        service.store.credentials(service.store.get(account.id))


def test_cancel_during_profile_verification_never_imports(composio):
    service, accounts, gateway = composio
    accounts.save_key("private-project-key")
    started = accounts.start("owner", "gmail")
    identifier = started["login_id"]
    accounts.pending[identifier].next_poll = 0
    entered, release = threading.Event(), threading.Event()

    def block():
        """Pause only the network verification so cancellation can race completion."""
        entered.set()
        assert release.wait(5)

    gateway.hook = block
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(accounts.poll, identifier, "owner")
        assert entered.wait(5)
        accounts.cancel(identifier, "owner")
        release.set()
        with pytest.raises(ConnectorError):
            result.result()
    assert service.store.list() == []


def test_dispatch_rechecks_identity_and_rejects_old_key_generation(composio):
    service, accounts, gateway = composio
    accounts.save_key("private-project-key")
    account = complete(accounts)
    provider = service.providers.get("gmail")
    credentials = service.vault.open(account, service.store.credentials(account))
    context = CallContext(
        account=account, deadline=time.monotonic() + 45, cancelled=threading.Event()
    )
    capability = provider.manifest.capabilities[1]
    assert provider.invoke(capability, {}, credentials, context).next_cursor == "page2"
    gateway.subject = "another@example.test"
    with pytest.raises(ConnectorError, match="account_changed"):
        provider.invoke(capability, {}, credentials, context)
    accounts.save_key("replacement-project-key")
    assert service.store.get(account.id).state == "disconnected"
    with pytest.raises(ConnectorError, match="needs_reauth"):
        provider.invoke(capability, {}, credentials, context)


def test_closed_translation_and_hosted_link_origin():
    schema = {
        "properties": {"query": {}, "max_results": {}, "user_id": {}},
        "required": ["user_id"],
    }
    assert tool_arguments("gmail", "fetch_emails", {"query": "is:unread"}, schema) == {
        "query": "is:unread",
        "max_results": 20,
        "user_id": "me",
    }
    with pytest.raises(ConnectorError):
        tool_arguments("gmail", "fetch_emails", {"user_id": "other"}, schema)
    with pytest.raises(ConnectorError):
        tool_arguments(
            "outlook",
            "list_messages",
            {"nextLink": "https://attacker.test/v1.0/me/messages?$skip=1"},
            {"properties": {"top": {}, "skip": {}}},
        )
    assert login_url("https://connect.composio.dev/link/abc")
    for url in [
        "https://connect.composio.dev.attacker.test/link/a",
        "http://connect.composio.dev/link/a",
    ]:
        with pytest.raises(ConnectorError):
            login_url(url)


def test_key_route_requires_privileged_scope_and_never_returns_key(composio):
    from app.main import app

    service, accounts, gateway = composio
    client = TestClient(app)
    ordinary = {
        "Authorization": "Bearer "
        + mint_control_token(
            os.environ["MONAW_CONTROL_SECRET"],
            scopes={"settings:write", "settings:read"},
        )
    }
    assert (
        client.put(
            "/api/connectors/composio/key",
            headers=ordinary,
            json={"api_key": "private-project-key"},
        ).status_code
        == 403
    )
    privileged = {
        "Authorization": "Bearer "
        + mint_control_token(
            os.environ["MONAW_CONTROL_SECRET"], scopes={"connectors:credentials"}
        )
    }
    response = client.put(
        "/api/connectors/composio/key",
        headers=privileged,
        json={"api_key": "private-project-key"},
    )
    assert response.status_code == 200
    assert "private-project-key" not in response.text
    assert client.get(
        "/api/connectors/composio/configuration", headers=ordinary
    ).json() == {"configured": True}


def test_sdk_wire_contract_pins_account_and_disables_unreviewed_execution(monkeypatch):
    from app.connectors import composio_gateway as module

    requests = []

    def respond(request):
        """Exercise the pinned generated SDK against synthetic HTTP responses."""
        requests.append(request)
        if request.url.path.endswith("/auth_configs"):
            return httpx.Response(
                200,
                json={
                    "auth_config": {
                        "id": "ac_test",
                        "auth_scheme": "OAUTH2",
                        "is_composio_managed": True,
                    },
                    "toolkit": {"slug": "gmail"},
                },
            )
        if request.url.path.endswith("/link"):
            return httpx.Response(
                200,
                json={
                    "connected_account_id": "ca_test",
                    "redirect_url": "https://connect.composio.dev/link/test",
                    "link_token": "test",
                },
            )
        if request.url.path.endswith("/execute"):
            return httpx.Response(
                200,
                json={
                    "result_type": "completed",
                    "data": {"emailAddress": "alice@example.test"},
                    "log_id": "log_test",
                },
            )
        if "/tools/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "slug": "GMAIL_GET_PROFILE",
                    "toolkit": {"slug": "gmail"},
                    "input_parameters": {
                        "type": "object",
                        "properties": {"user_id": {}},
                        "required": ["user_id"],
                    },
                },
            )
        if request.method == "POST":
            return httpx.Response(200, json={"session_id": "ts_test", "config": {}})
        return httpx.Response(
            200, json={"items": [], "next_cursor": None, "total_pages": 0}
        )

    monkeypatch.setattr(
        module, "BoundedTransport", lambda: httpx.MockTransport(respond)
    )
    gateway = ComposioGateway()
    gateway.validate("private-project-key")
    assert (
        gateway.session(
            "private-project-key", "local-user", "gmail", "ca_test", "ac_test"
        )
        == "ts_test"
    )
    payload = json.loads(requests[-1].content)
    assert payload["connected_accounts"] == {"gmail": ["ca_test"]}
    assert payload["instant"] is False
    assert payload["proxy_execute"] == {"enable": False}
    assert payload["workbench"]["enable"] is False
    assert payload["tools"]["gmail"]["enable"] == list(module.TOOLS["gmail"].values())
    assert requests[-1].headers["x-api-key"] == "private-project-key"

    assert gateway.auth_config("private-project-key", "gmail") == "ac_test"
    auth = json.loads(requests[-1].content)["auth_config"]
    assert auth["type"] == "use_composio_managed_auth"
    assert (
        "https://www.googleapis.com/auth/gmail.readonly"
        in auth["credentials"]["scopes"]
    )
    assert gateway.link("private-project-key", "ts_test", "gmail")[0] == "ca_test"
    assert gateway.execute(
        "private-project-key", "ts_test", "gmail", "get_profile", {}
    ) == {"emailAddress": "alice@example.test"}
    assert json.loads(requests[-1].content)["arguments"] == {"user_id": "me"}


def test_catalog_matches_reviewed_schemas_and_cannot_override_account_selectors():
    """Check all new tool bindings against captured public schemas, without live calls."""
    import json
    from pathlib import Path
    from app.connectors.composio_catalog import FIXED_ARGUMENTS, SCOPES, TOOLS

    schemas = json.loads(
        (Path(__file__).parent / "fixtures/composio_reviewed_schemas.json").read_text(
            encoding="utf-8"
        )
    )
    for manifest in MANIFESTS:
        assert manifest.connection_method == "composio"
        assert manifest.category != "other"
        assert manifest.id in SCOPES
        for capability in manifest.capabilities:
            slug = TOOLS[manifest.id][capability.id]
            assert capability.action == "read"
            if slug not in schemas:
                continue  # Existing mail aliases have separate wire-contract coverage.
            arguments = {
                key: "12345678-1234-1234-1234-123456789abc"
                for key in capability.parameters["required"]
            }
            result = tool_arguments(
                manifest.id, capability.id, arguments, schemas[slug]
            )
            for name, value in FIXED_ARGUMENTS[manifest.id, capability.id].items():
                assert result[name] == value
            with pytest.raises(ConnectorError, match="invalid_arguments"):
                tool_arguments(
                    manifest.id,
                    capability.id,
                    {**arguments, "user_id": "another-account"},
                    schemas[slug],
                )
            for name, field in capability.parameters["properties"].items():
                if field.get("maximum") == 50:
                    assert result[name] == 20
                    with pytest.raises(ConnectorError, match="invalid_arguments"):
                        tool_arguments(
                            manifest.id,
                            capability.id,
                            {**arguments, name: 51},
                            schemas[slug],
                        )


def test_new_provider_identity_isolation_and_key_removal(composio, monkeypatch):
    """Link every new provider, project profiles safely, and invalidate all bindings on key removal."""
    from app.connectors.composio import profile, next_cursor

    service, accounts, gateway = composio
    profiles = {
        "googlecalendar": {
            "account_id": "calendar@example.test",
            "email": "calendar@example.test",
        },
        "googledrive": {
            "user": {"permissionId": "drive-user", "emailAddress": "drive@example.test"}
        },
        "one_drive": {
            "id": "microsoft-user",
            "userPrincipalName": "files@example.test",
        },
        "notion": {"id": "notion-bot", "bot": {"workspace_name": "Notes"}},
        "slack": {
            "ok": True,
            "team_id": "T1",
            "user_id": "U1",
            "team": "Team",
            "user": "Alice",
        },
        "microsoft_teams": {"id": "teams-user", "mail": "teams@example.test"},
        "todoist": {
            "id": "todo-user",
            "email": "todo@example.test",
            "token": "must-not-be-exposed",
        },
        "linear": {"user": {"id": "linear-user", "email": "linear@example.test"}},
    }

    def execute(key, session, provider, capability, arguments, remaining=lambda: 20):
        """Return provider-shaped identity envelopes, including a sensitive Todoist field."""
        return {"successful": True, "data": profiles[provider]}

    monkeypatch.setattr(gateway, "execute", execute)
    accounts.save_key("private-project-key")
    for provider_id in profiles:
        started = accounts.start("owner", provider_id)
        accounts.pending[started["login_id"]].next_poll = 0
        assert accounts.poll(started["login_id"], "owner")["status"] == "connected"
        account = next(
            item for item in service.store.list() if item.provider_id == provider_id
        )
        adapter = service.providers.get(provider_id)
        credential = service.vault.open(account, service.store.credentials(account))
        context = CallContext(account=account, deadline=time.monotonic() + 45)
        result = adapter.invoke(
            adapter.manifest.capabilities[0], {}, credential, context
        )
        assert result.data == {"subject": account.subject, "label": account.label}
        assert "must-not-be-exposed" not in str(result.data)
    assert (
        profile("slack", {**profiles["slack"], "team_id": "T2"})[0]
        != profile("slack", profiles["slack"])[0]
    )
    with pytest.raises(ConnectorError):
        profile("slack", {"ok": False, "error": "denied"})
    assert (
        next_cursor({"response_metadata": {"next_cursor": "slack-next"}})
        == "slack-next"
    )
    assert (
        next_cursor({"page_info": {"hasNextPage": True, "endCursor": "linear-next"}})
        == "linear-next"
    )
    accounts.remove_key()
    assert all(item.state == "disconnected" for item in service.store.list())


def test_gmail_managed_read_scope_block_is_reported_before_creating_a_session(composio):
    """Do not launch a consent flow the upstream provider has confirmed cannot succeed."""
    service, accounts, gateway = composio
    gmail = next(manifest for manifest in MANIFESTS if manifest.id == "gmail")
    service.providers.get("gmail").manifest = gmail
    accounts.save_key("private-project-key")
    before = list(gateway.calls)
    with pytest.raises(ConnectorError, match="provider_unavailable"):
        accounts.start("owner", "gmail")
    assert gateway.calls == before
    assert "not approved" in gmail.setup_notice
    assert all(
        manifest.setup_notice
        for manifest in MANIFESTS
        if manifest.id in {"gmail", "googlecalendar", "googledrive"}
    )
