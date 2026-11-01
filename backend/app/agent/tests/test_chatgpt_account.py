"""Sign in with ChatGPT: PKCE sign-in, token storage and refresh, and plan model calls."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.agent import chatgpt_account as account_mod
from app.agent.chatgpt_account import (
    DYNAMIC_CLIENT_ID,
    PLAN_SCOPE,
    ChatGPTAccountError,
    ChatGPTAccountService,
    plan_error_from_response,
)
from app.agent.llm_client import LLMClient, LLMResponse, ResponseFailedError, _stateless_input
from app.agent.retry import _default_retryable

GRANTED = f"openid profile email offline_access resource.invoke {PLAN_SCOPE}"
ISSUED_CLIENT = "client_issued_123"


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class OpenAIAuth:
    """Stands in for auth.openai.com: signs ID tokens and records token requests."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.requests: list[dict] = []
        self.responses: list[FakeResponse] = []
        self.subject = "user-1"

    def id_token(self, *, client_id=ISSUED_CLIENT, nonce="", subject=None, issuer="https://auth.openai.com"):
        now = int(time.time())
        return jwt.encode({
            "iss": issuer, "aud": client_id, "sub": subject or self.subject, "email": "kai@example.com",
            "nonce": nonce, "iat": now, "exp": now + 3600,
        }, self.key, algorithm="RS256")

    def tokens(self, *, nonce="", scope=GRANTED, client_id=ISSUED_CLIENT, access="access-1", refresh="refresh-1", **claims):
        return {
            "access_token": access, "refresh_token": refresh, "token_type": "Bearer", "expires_in": 3600,
            "scope": scope, "id_token": self.id_token(client_id=client_id, nonce=nonce, **claims),
        }

    def post(self, url, data=None, timeout=None):
        self.requests.append({"url": url, **(data or {})})
        return self.responses.pop(0)


@pytest.fixture
def auth(monkeypatch):
    fake = OpenAIAuth()
    monkeypatch.setattr(account_mod.httpx, "post", fake.post)
    return fake


@pytest.fixture
def service(tmp_path, auth):
    svc = ChatGPTAccountService(tmp_path / "chatgpt-account")
    svc._openid_config = {
        "issuer": "https://auth.openai.com", "jwks_uri": "https://auth.openai.com/jwks",
        "revocation_endpoint": "https://auth.openai.com/oauth/revoke",
    }
    svc._jwks_client = SimpleNamespace(get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=auth.key.public_key()))
    yield svc
    svc._stop_pending()


def _query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


def _sign_in(service, auth, *, scope=GRANTED, **claims) -> dict[str, str]:
    params = _query(service.start_login()["auth_url"])
    auth.responses.append(FakeResponse(200, auth.tokens(nonce=params["nonce"], scope=scope, **claims)))
    message = service._complete_login({"state": params["state"], "code": "code-1", "client_id": ISSUED_CLIENT,
                                       "scope": scope})
    return {**params, "message": message}


def test_first_sign_in_registers_monaw_with_pkce_on_a_loopback_callback(service, auth):
    login = service.start_login()
    params = _query(login["auth_url"])

    assert login["auth_url"].startswith("https://auth.openai.com/api/accounts/authorize?")
    assert params["client_id"] == DYNAMIC_CLIENT_ID
    assert params["agent_name_hint"] == "Monaw"
    assert params["ext_agent_host_id"].startswith("urn:uuid:")
    assert params["response_type"] == "code"
    assert params["scope"].split() == ["openid", "profile", "email", "offline_access", "resource.invoke", PLAN_SCOPE]
    assert params["resource"] == "https://api.openai.com/v1"
    assert urlparse(params["redirect_uri"]).hostname == "127.0.0.1"
    assert urlparse(params["redirect_uri"]).path == "/auth/callback"
    assert params["code_challenge_method"] == "S256"
    verifier = service._pending.verifier
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert params["code_challenge"] == expected
    assert service.status()["login_pending"] is True


def test_completed_sign_in_keeps_the_issued_client_and_encrypted_tokens(service, auth):
    params = _sign_in(service, auth)

    assert params["message"] == "Signed in to Monaw with ChatGPT."
    exchange = auth.requests[-1]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["client_id"] == ISSUED_CLIENT
    assert exchange["code_verifier"] and exchange["redirect_uri"] == params["redirect_uri"]
    assert exchange["resource"] == "https://api.openai.com/v1"
    status = service.status()
    assert status["connected"] and status["plan_usage"] and status["email"] == "kai@example.com"
    assert status["login_pending"] is False and status["login_error"] == ""
    assert json.loads((service.home / "registration.json").read_text())["client_id"] == ISSUED_CLIENT
    raw = (service.home / "credentials.dat").read_bytes()
    assert b"refresh-1" not in raw or not hasattr(account_mod.os, "add_dll_directory")
    assert service.access_token() == "access-1"


def test_returning_sign_in_reuses_the_client_and_hints_the_account(service, auth):
    _sign_in(service, auth)
    params = _query(service.start_login()["auth_url"])

    assert params["client_id"] == ISSUED_CLIENT
    assert "agent_name_hint" not in params
    assert params["id_token_hint"] and params["login_hint"] == "kai@example.com"
    assert params["ext_agent_host_id"] == json.loads((service.home / "host.json").read_text())["ext_agent_host_id"]


@pytest.mark.parametrize("query, error", [
    ({"error": "access_denied"}, "Sign-in was cancelled."),
    ({"code": "c"}, "ChatGPT did not finish registering Monaw. Try again."),
    ({"code": "c", "client_id": DYNAMIC_CLIENT_ID}, "ChatGPT did not finish registering Monaw. Try again."),
    ({"client_id": ISSUED_CLIENT}, "ChatGPT did not return a sign-in code. Try again."),
])
def test_incomplete_callbacks_are_rejected_without_saving_tokens(service, auth, query, error):
    params = _query(service.start_login()["auth_url"])
    assert service._complete_login({"state": params["state"], **query}) == error
    assert service.status()["login_error"] == error
    assert not service.status()["connected"]
    assert auth.requests == []


def test_a_callback_with_the_wrong_state_is_ignored(service, auth):
    service.start_login()
    assert service._complete_login({"state": "forged", "code": "c", "client_id": ISSUED_CLIENT}) == \
        "This sign-in link is no longer valid."
    assert service.status()["login_pending"] is True
    assert auth.requests == []


@pytest.mark.parametrize("claims", [{"nonce": "wrong"}, {"client_id": "other-client"}, {"issuer": "https://evil.example"}])
def test_an_id_token_that_fails_verification_is_rejected(service, auth, claims):
    params = _query(service.start_login()["auth_url"])
    tokens = auth.tokens(nonce=params["nonce"])
    tokens["id_token"] = auth.id_token(
        nonce=claims.get("nonce", params["nonce"]), client_id=claims.get("client_id", ISSUED_CLIENT),
        issuer=claims.get("issuer", "https://auth.openai.com"))
    auth.responses.append(FakeResponse(200, tokens))
    message = service._complete_login({"state": params["state"], "code": "c", "client_id": ISSUED_CLIENT})
    assert message == "ChatGPT sign-in could not be verified. Try again."
    assert not service.status()["connected"]


def test_a_different_account_on_a_saved_registration_is_not_merged(service, auth):
    _sign_in(service, auth)
    service._clear_credentials()
    params = _sign_in(service, auth, subject="someone-else")
    assert "not the one Monaw was registered with" in params["message"]
    assert not service.status()["connected"]
    assert _query(service.start_login()["auth_url"])["client_id"] == DYNAMIC_CLIENT_ID


def test_declined_plan_use_keeps_the_sign_in_but_blocks_model_calls(service, auth):
    _sign_in(service, auth, scope="openid profile email offline_access")
    status = service.status()
    assert status["connected"] and status["plan_usage"] is False
    assert "isn't enabled" in status["usage_error"]
    with pytest.raises(ChatGPTAccountError) as caught:
        service.access_token()
    assert caught.value.code == "plan_use_not_enabled" and caught.value.retryable is False
    assert _query(service.start_login()["auth_url"])["prompt"] == "consent"


def test_access_token_refreshes_near_expiry_and_keeps_the_rotated_tokens(service, auth):
    _sign_in(service, auth)
    record = service._credentials()
    record["saved_at"] = time.time() - 3500
    service._save_credentials(record)
    auth.responses.append(FakeResponse(200, {"access_token": "access-2", "refresh_token": "refresh-2",
                                             "expires_in": 3600, "scope": GRANTED, "token_type": "Bearer"}))

    assert service.access_token() == "access-2"
    refresh = auth.requests[-1]
    assert refresh == {"url": account_mod.TOKEN_URL, "grant_type": "refresh_token", "client_id": ISSUED_CLIENT,
                       "refresh_token": "refresh-1", "resource": "https://api.openai.com/v1"}
    saved = service._credentials()
    assert saved["refresh_token"] == "refresh-2" and saved["id_token"]
    assert service.access_token() == "access-2"
    assert len(auth.requests) == 2


def test_a_dead_refresh_token_signs_out_but_a_server_error_keeps_the_sign_in(service, auth):
    _sign_in(service, auth)
    auth.responses.append(FakeResponse(500, {"error": "server_error"}))
    with pytest.raises(ChatGPTAccountError) as caught:
        service.access_token(force_refresh=True)
    assert caught.value.retryable is True
    assert service.status()["connected"]

    auth.responses.append(FakeResponse(400, {"error": "invalid_grant"}))
    with pytest.raises(ChatGPTAccountError) as caught:
        service.access_token(force_refresh=True)
    assert caught.value.retryable is False and "Sign in again" in str(caught.value)
    assert not service.status()["connected"]


def test_sign_out_revokes_the_refresh_token_and_keeps_the_registration(service, auth):
    _sign_in(service, auth)
    auth.responses.append(FakeResponse(200))
    status = service.logout()

    assert auth.requests[-1] == {"url": "https://auth.openai.com/oauth/revoke", "token": "refresh-1",
                                 "token_type_hint": "refresh_token", "client_id": ISSUED_CLIENT}
    assert not status["connected"] and status["login_error"] == ""
    assert (service.home / "registration.json").exists() and (service.home / "host.json").exists()


def test_account_routes_never_return_tokens(service, auth, monkeypatch):
    import app.api.routes.openai_account as routes

    _sign_in(service, auth)
    monkeypatch.setattr(routes, "chatgpt_account", service)
    status = asyncio.run(routes.account_status())
    assert status["connected"] is True
    serialized = json.dumps(status)
    for secret in ("access-1", "refresh-1", "id_token", "access_token", "refresh_token", ISSUED_CLIENT):
        assert secret not in serialized
    assert asyncio.run(routes.account_login())["auth_url"].startswith("https://auth.openai.com/")
    auth.responses.append(FakeResponse(200))
    assert asyncio.run(routes.account_logout())["connected"] is False


def test_unconfirmed_sign_out_says_so(service, auth):
    _sign_in(service, auth)
    auth.responses.append(FakeResponse(503))
    status = service.logout()
    assert not status["connected"]
    assert "did not confirm" in status["login_error"]


@pytest.mark.parametrize("status, code, retryable, text", [
    (429, "subscription_sharing_usage_limit_exceeded", False, "usage limit"),
    (403, "subscription_sharing_user_not_eligible", False, "can't use its plan"),
    (400, "subscription_sharing_unsupported_capability", False, "don't support"),
    (503, "subscription_sharing_usage_unavailable", True, "temporarily unavailable"),
    (503, "", True, "temporarily unavailable"),
    (401, "subscription_sharing_invalid_user", False, "Sign in again"),
    (403, "chatpass_v2_scope_not_authorized", False, "refused"),
])
def test_plan_errors_become_clear_messages_and_only_transient_ones_retry(status, code, retryable, text):
    error = plan_error_from_response(status, code, "detail")
    assert error is not None and text in str(error)
    assert _default_retryable(error) is retryable


def test_stateless_input_drops_unreplayable_reasoning_and_item_ids():
    items = [
        {"role": "user", "content": "hi"},
        {"type": "reasoning", "id": "rs_1", "summary": []},
        {"type": "reasoning", "id": "rs_2", "summary": [], "encrypted_content": "enc"},
        {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "search", "arguments": "{}", "status": "completed"},
        {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
    ]
    assert _stateless_input(items) == [
        {"role": "user", "content": "hi"},
        {"type": "reasoning", "summary": [], "encrypted_content": "enc"},
        {"type": "function_call", "call_id": "call_1", "name": "search", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
    ]


class FakeAccount:
    def __init__(self, tokens):
        self.tokens = list(tokens)
        self.calls = []

    def access_token(self, *, force_refresh=False):
        self.calls.append(force_refresh)
        return self.tokens.pop(0)


def _client(monkeypatch, account, outcomes):
    monkeypatch.setattr(account_mod, "chatgpt_account", account)
    client = LLMClient("codex", "gpt-6.1-sol", "", reasoning_effort="ultra")
    sent = []

    async def stream(kwargs, stream_callback, reasoning_callback=None):
        sent.append({"kwargs": kwargs, "token": client._openai_client.api_key})
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(client, "_openai_responses_stream", stream)
    return client, sent


def test_plan_calls_stream_without_storing_and_carry_encrypted_reasoning(monkeypatch):
    account = FakeAccount(["access-1"])
    client, sent = _client(monkeypatch, account, [LLMResponse(content="Hello")])
    messages = [{"role": "user", "content": "Hey"}, {"type": "reasoning", "id": "rs_1", "summary": []}]

    response = asyncio.run(client._codex_chat(messages, [], "Be brief.", None))

    assert response.content == "Hello" and response.usage.source == "chatgpt_account"
    kwargs = sent[0]["kwargs"]
    assert sent[0]["token"] == "access-1"
    assert kwargs["store"] is False
    assert kwargs["include"] == ["reasoning.encrypted_content"]
    assert kwargs["instructions"] == "Be brief."
    assert kwargs["reasoning"]["effort"] == "max"
    assert kwargs["input"] == [{"role": "user", "content": "Hey"}]
    assert not {"temperature", "max_output_tokens", "previous_response_id", "metadata"} & set(kwargs)


def test_tool_calls_are_read_from_streamed_items_when_completed_output_is_empty(monkeypatch):
    # Observed on the plan route: response.completed arrives with output=[],
    # so the call and the reasoning exist only as output_item.done events.
    reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc"}
    call = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "fs_list",
            "arguments": '{"path":"."}', "status": "completed"}

    async def fake_stream():
        yield SimpleNamespace(type="response.output_item.added", output_index=1, item=SimpleNamespace(**{**call, "arguments": ""}))
        yield SimpleNamespace(type="response.output_item.done", output_index=0, item=reasoning)
        yield SimpleNamespace(type="response.function_call_arguments.done", output_index=1, arguments='{"path":"."}', name="fs_list")
        yield SimpleNamespace(type="response.output_item.done", output_index=1, item=call)
        yield SimpleNamespace(type="response.completed", response=SimpleNamespace(
            status="completed", output=[], error=None, incomplete_details=None, usage=None))

    class FakeResponses:
        async def create(self, **kwargs):
            return fake_stream()

    client = LLMClient("openai", "gpt-6.1-sol", "key")
    client._openai_client = SimpleNamespace(responses=FakeResponses())
    response = asyncio.run(client._openai_responses_stream({"model": "gpt-6.1-sol"}, None))

    assert [(c.call_id, c.tool_name, c.arguments) for c in response.tool_calls] == [("call_1", "fs_list", {"path": "."})]
    assert response.finish_reason == "tool_calls"
    assert [item["type"] for item in response.provider_messages] == ["reasoning", "function_call"]
    assert response.provider_messages[0]["encrypted_content"] == "enc"


def test_a_rejected_token_is_renewed_once_before_failing(monkeypatch):
    import httpx
    import openai

    def status_error(code):
        response = httpx.Response(401, request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
        return openai.AuthenticationError("unauthorized", response=response, body={"error": {"code": code}})

    account = FakeAccount(["stale", "fresh"])
    client, sent = _client(monkeypatch, account, [status_error(""), LLMResponse(content="ok")])
    assert asyncio.run(client._codex_chat([{"role": "user", "content": "x"}], [], "", None)).content == "ok"
    assert account.calls == [False, True]
    assert [call["token"] for call in sent] == ["stale", "fresh"]


def test_a_usage_limit_mid_stream_stops_without_retrying(monkeypatch):
    account = FakeAccount(["access-1"] * 4)
    failure = ResponseFailedError("limit", code="subscription_sharing_usage_limit_exceeded")
    client, sent = _client(monkeypatch, account, [failure, LLMResponse(content="never")])
    with pytest.raises(ChatGPTAccountError) as caught:
        asyncio.run(client.chat_with_tools([{"role": "user", "content": "x"}], []))
    assert "usage limit" in str(caught.value)
    assert len(sent) == 1
