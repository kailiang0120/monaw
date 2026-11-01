"""Sign in with ChatGPT and use the account's plan for model calls.

Follows OpenAI's open-source flow (developers.openai.com/siwc/token-sharing-open-source):
a PKCE sign-in returns to a loopback callback, the first sign-in registers Monaw
and issues it a client ID, and model calls go to the public Responses API with the
account's access token. Requests need `store: false` and `stream: true`.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from app.agent.runtime_paths import MONAW_HOME_DIR

logger = logging.getLogger(__name__)

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
OPENID_CONFIGURATION_URL = f"{ISSUER}/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = f"openid profile email offline_access resource.invoke {PLAN_SCOPE}"
DYNAMIC_CLIENT_ID = "dynamic_agent_client"
AGENT_NAME = "Monaw"
CALLBACK_PATH = "/auth/callback"
PREFERRED_PORT = 1455
USAGE_URL = "https://chatgpt.com/#settings"
_LOGIN_TIMEOUT_SECONDS = 10 * 60
# Refresh this long before the hour-long access token expires.
_REFRESH_MARGIN_SECONDS = 5 * 60
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
# Refresh errors that mean the refresh token can no longer be used.
_DEAD_REFRESH_ERRORS = {
    "invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
    "refresh_token_invalidated", "refresh_token_reused",
}


class ChatGPTAccountError(RuntimeError):
    """A sign-in or plan-usage failure with a message fit for the user."""

    def __init__(self, message: str, *, code: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        # Read by retry.py: only transient failures are retried.
        self.retryable = retryable


@dataclass
class _PendingLogin:
    state: str
    nonce: str
    verifier: str
    redirect_uri: str
    client_id: str
    server: ThreadingHTTPServer
    timer: threading.Timer | None = None
    started_at: float = field(default_factory=time.time)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _protect(data: bytes) -> bytes:
    """Encrypt for this Windows user with DPAPI; elsewhere the file stays owner-only."""
    if os.name != "nt":
        return data
    import win32crypt

    return win32crypt.CryptProtectData(data, "Monaw ChatGPT account", None, None, None, 0)


def _unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        return data
    import win32crypt

    return win32crypt.CryptUnprotectData(data, None, None, None, 0)[1]


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_bytes(data)
    if os.name != "nt":
        os.chmod(temp, 0o600)
    os.replace(temp, path)


def _granted_scopes(value: Any) -> list[str]:
    return sorted(set(str(value or "").split()))


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("code") or error.get("type") or "")
        return str(error or body.get("code") or "")
    return ""


class ChatGPTAccountService:
    def __init__(self, home: Path | None = None) -> None:
        self.home = home or MONAW_HOME_DIR / "chatgpt-account"
        self._lock = threading.RLock()
        self._pending: _PendingLogin | None = None
        self._login_error = ""
        self._openid_config: dict[str, Any] | None = None
        self._jwks_client: Any = None

    # -- Files -----------------------------------------------------------------

    @property
    def _host_path(self) -> Path:
        return self.home / "host.json"

    @property
    def _registration_path(self) -> Path:
        return self.home / "registration.json"

    @property
    def _credentials_path(self) -> Path:
        return self.home / "credentials.dat"

    def _host_id(self) -> str:
        """One stable, opaque ID for this computer, kept across sign-outs."""
        try:
            value = json.loads(self._host_path.read_text(encoding="utf-8")).get("ext_agent_host_id", "")
            if isinstance(value, str) and value.startswith("urn:uuid:"):
                return value
        except (OSError, ValueError, AttributeError):
            pass
        value = f"urn:uuid:{uuid.uuid4()}"
        _write_atomic(self._host_path, json.dumps({"ext_agent_host_id": value}).encode("utf-8"))
        return value

    def _registration(self) -> dict[str, Any]:
        """The issued client ID and the account it belongs to, kept across sign-outs."""
        try:
            value = json.loads(self._registration_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) and value.get("client_id") else {}
        except (OSError, ValueError):
            return {}

    def _credentials(self) -> dict[str, Any]:
        try:
            value = json.loads(_unprotect(self._credentials_path.read_bytes()).decode("utf-8"))
            return value if isinstance(value, dict) and value.get("access_token") else {}
        except FileNotFoundError:
            return {}
        except Exception:
            logger.warning("Could not read the saved ChatGPT sign-in; sign in again")
            return {}

    def _save_credentials(self, record: dict[str, Any]) -> None:
        _write_atomic(self._credentials_path, _protect(json.dumps(record).encode("utf-8")))

    def _clear_credentials(self) -> None:
        self._credentials_path.unlink(missing_ok=True)

    # -- Status ----------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            credentials = self._credentials()
            plan_usage = PLAN_SCOPE in credentials.get("scopes", [])
            return {
                "connected": bool(credentials),
                "email": str(credentials.get("email") or ""),
                "plan_usage": plan_usage,
                "plan": "",
                "limits": [],
                "usage_error": (
                    "" if not credentials or plan_usage
                    else "ChatGPT plan use isn't enabled for Monaw. Sign in again and allow it."
                ),
                "usage_url": USAGE_URL,
                "login_pending": self._pending is not None,
                "login_error": self._login_error,
            }

    # -- Sign-in ---------------------------------------------------------------

    def start_login(self) -> dict[str, str]:
        with self._lock:
            self._stop_pending()
            self._login_error = ""
            registration = self._registration()
            credentials = self._credentials()
            client_id = str(registration.get("client_id") or DYNAMIC_CLIENT_ID)
            server = self._start_callback_server()
            redirect_uri = f"http://127.0.0.1:{server.server_address[1]}{CALLBACK_PATH}"
            verifier = _b64url(secrets.token_bytes(32))
            pending = _PendingLogin(
                state=_b64url(secrets.token_bytes(24)),
                nonce=_b64url(secrets.token_bytes(24)),
                verifier=verifier,
                redirect_uri=redirect_uri,
                client_id=client_id,
                server=server,
            )
            params = {
                "client_id": client_id,
                "ext_agent_host_id": self._host_id(),
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": SCOPES,
                "resource": RESOURCE,
                "state": pending.state,
                "nonce": pending.nonce,
                "code_challenge_method": "S256",
                "code_challenge": _b64url(hashlib.sha256(verifier.encode("ascii")).digest()),
            }
            if client_id == DYNAMIC_CLIENT_ID:
                params["agent_name_hint"] = AGENT_NAME
            else:
                if credentials.get("id_token"):
                    params["id_token_hint"] = credentials["id_token"]
                if registration.get("email"):
                    params["login_hint"] = registration["email"]
                if credentials and PLAN_SCOPE not in credentials.get("scopes", []):
                    # Plan use was declined before; ask again rather than reuse the grant.
                    params["prompt"] = "consent"
            pending.timer = threading.Timer(_LOGIN_TIMEOUT_SECONDS, self._expire, args=(pending.state,))
            pending.timer.daemon = True
            self._pending = pending
            threading.Thread(target=server.serve_forever, name="chatgpt-sign-in", daemon=True).start()
            pending.timer.start()
            return {"auth_url": f"{AUTHORIZE_URL}?{urlencode(params)}", "login_id": pending.state}

    def _start_callback_server(self) -> ThreadingHTTPServer:
        service = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                parsed = urlparse(self.path)
                if parsed.path != CALLBACK_PATH:
                    self.send_error(404)
                    return
                query = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}
                message = service._complete_login(query)
                body = (
                    "<!doctype html><meta charset='utf-8'><title>Monaw</title>"
                    "<body style='font-family:system-ui;padding:40px'>"
                    f"<h2>{html.escape(message)}</h2><p>You can close this tab and return to Monaw.</p></body>"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:
                return

        try:
            return ThreadingHTTPServer(("127.0.0.1", PREFERRED_PORT), CallbackHandler)
        except OSError:
            # Only the port may change between sign-ins.
            return ThreadingHTTPServer(("127.0.0.1", 0), CallbackHandler)

    def _expire(self, state: str) -> None:
        with self._lock:
            if self._pending is not None and self._pending.state == state:
                self._login_error = "Sign-in timed out. Try again."
                self._stop_pending()

    def _stop_pending(self) -> None:
        pending, self._pending = self._pending, None
        if pending is not None:
            if pending.timer is not None:
                pending.timer.cancel()
            # shutdown() waits for serve_forever, which may be running this callback.
            threading.Thread(target=pending.server.shutdown, daemon=True).start()

    def _complete_login(self, query: dict[str, str]) -> str:
        with self._lock:
            pending = self._pending
            if pending is None or not secrets.compare_digest(query.get("state", ""), pending.state):
                return "This sign-in link is no longer valid."
            try:
                self._finish_login(pending, query)
                self._login_error = ""
                return "Signed in to Monaw with ChatGPT."
            except ChatGPTAccountError as exc:
                self._login_error = str(exc)
                return str(exc)
            except Exception:
                logger.exception("ChatGPT sign-in failed")
                self._login_error = "Sign-in failed. Try again."
                return self._login_error
            finally:
                self._stop_pending()

    def _finish_login(self, pending: _PendingLogin, query: dict[str, str]) -> None:
        if query.get("error"):
            if query["error"] == "access_denied":
                raise ChatGPTAccountError("Sign-in was cancelled.", code="access_denied")
            raise ChatGPTAccountError(f"Sign-in failed: {query['error']}.", code=query["error"])
        callback_client = query.get("client_id", "")
        if pending.client_id == DYNAMIC_CLIENT_ID:
            if not callback_client or callback_client == DYNAMIC_CLIENT_ID:
                raise ChatGPTAccountError("ChatGPT did not finish registering Monaw. Try again.")
            client_id = callback_client
        else:
            if callback_client and callback_client != pending.client_id:
                raise ChatGPTAccountError("ChatGPT returned a different app registration. Try again.")
            client_id = pending.client_id
        if not query.get("code"):
            raise ChatGPTAccountError("ChatGPT did not return a sign-in code. Try again.")

        response = httpx.post(TOKEN_URL, data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": query["code"],
            "code_verifier": pending.verifier,
            "redirect_uri": pending.redirect_uri,
            "resource": RESOURCE,
        }, timeout=_HTTP_TIMEOUT)
        if response.status_code != 200:
            code = _error_code(response)
            logger.warning("ChatGPT code exchange failed: HTTP %s %s", response.status_code, code)
            raise ChatGPTAccountError("ChatGPT sign-in could not be completed. Try again.", code=code)
        tokens = response.json()
        claims = self._verify_id_token(str(tokens.get("id_token") or ""), client_id=client_id, nonce=pending.nonce)

        registration = self._registration()
        if registration.get("client_id") == client_id and registration.get("subject") not in {None, "", claims["sub"]}:
            # The issued client belongs to another account; register afresh next time.
            self._registration_path.unlink(missing_ok=True)
            raise ChatGPTAccountError(
                "That ChatGPT account is not the one Monaw was registered with. Sign in again to register this account.")
        email = str(claims.get("email") or "")
        _write_atomic(self._registration_path, json.dumps({
            "client_id": client_id, "subject": claims["sub"], "email": email, "issuer": ISSUER,
        }).encode("utf-8"))
        self._save_credentials(self._record(tokens, client_id=client_id, subject=claims["sub"], email=email))

    def _record(self, tokens: dict[str, Any], *, client_id: str, subject: str, email: str,
                previous: dict[str, Any] | None = None) -> dict[str, Any]:
        previous = previous or {}
        saved_at = time.time()
        return {
            "email": email,
            "issuer": ISSUER,
            "subject": subject,
            "client_id": client_id,
            "ext_agent_host_id": self._host_id(),
            "id_token": str(tokens.get("id_token") or previous.get("id_token") or ""),
            "access_token": str(tokens.get("access_token") or ""),
            "refresh_token": str(tokens.get("refresh_token") or previous.get("refresh_token") or ""),
            "token_type": str(tokens.get("token_type") or "Bearer"),
            "expires_in": int(tokens.get("expires_in") or 3600),
            "scopes": _granted_scopes(tokens.get("scope")) or previous.get("scopes", []),
            "saved_at": saved_at,
        }

    def _openid(self) -> dict[str, Any]:
        if self._openid_config is None:
            response = httpx.get(OPENID_CONFIGURATION_URL, timeout=_HTTP_TIMEOUT)
            response.raise_for_status()
            self._openid_config = response.json()
        return self._openid_config

    def _verify_id_token(self, id_token: str, *, client_id: str, nonce: str) -> dict[str, Any]:
        import jwt

        if not id_token:
            raise ChatGPTAccountError("ChatGPT did not return an ID token. Try again.")
        config = self._openid()
        if self._jwks_client is None:
            self._jwks_client = jwt.PyJWKClient(str(config["jwks_uri"]))
        try:
            key = self._jwks_client.get_signing_key_from_jwt(id_token)
            claims = jwt.decode(
                id_token, key.key, algorithms=["RS256", "ES256"], audience=client_id,
                issuer=str(config.get("issuer") or ISSUER), options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except jwt.PyJWTError as exc:
            logger.warning("ChatGPT ID token rejected: %s", exc)
            raise ChatGPTAccountError("ChatGPT sign-in could not be verified. Try again.") from exc
        if not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
            raise ChatGPTAccountError("ChatGPT sign-in could not be verified. Try again.")
        return claims

    # -- Tokens ----------------------------------------------------------------

    def access_token(self, *, force_refresh: bool = False) -> str:
        """A current access token that may use the plan, refreshing when near expiry."""
        with self._lock:
            credentials = self._credentials()
            if not credentials:
                raise ChatGPTAccountError("Sign in with ChatGPT in Settings > Connections first.", code="not_signed_in")
            if PLAN_SCOPE not in credentials.get("scopes", []):
                raise ChatGPTAccountError(
                    "ChatGPT plan use isn't enabled for Monaw. Sign in again in Settings and allow it.",
                    code="plan_use_not_enabled")
            expires_at = float(credentials.get("saved_at") or 0) + int(credentials.get("expires_in") or 0)
            if force_refresh or time.time() >= expires_at - _REFRESH_MARGIN_SECONDS:
                credentials = self._refresh(credentials)
            return str(credentials["access_token"])

    def _refresh(self, credentials: dict[str, Any]) -> dict[str, Any]:
        if not credentials.get("refresh_token"):
            self._clear_credentials()
            raise ChatGPTAccountError("Your ChatGPT sign-in expired. Sign in again in Settings.", code="refresh_token_missing")
        try:
            response = httpx.post(TOKEN_URL, data={
                "grant_type": "refresh_token",
                "client_id": credentials["client_id"],
                "refresh_token": credentials["refresh_token"],
                "resource": RESOURCE,
            }, timeout=_HTTP_TIMEOUT)
        except httpx.HTTPError as exc:
            raise ChatGPTAccountError("Could not reach ChatGPT to renew the sign-in.", retryable=True) from exc
        if response.status_code != 200:
            code = _error_code(response)
            if code in _DEAD_REFRESH_ERRORS:
                self._clear_credentials()
                raise ChatGPTAccountError("Your ChatGPT sign-in expired. Sign in again in Settings.", code=code)
            logger.warning("ChatGPT token refresh failed: HTTP %s %s", response.status_code, code)
            raise ChatGPTAccountError("Could not renew the ChatGPT sign-in.", code=code,
                                      retryable=response.status_code >= 500)
        record = self._record(response.json(), client_id=credentials["client_id"], subject=credentials["subject"],
                              email=credentials.get("email", ""), previous=credentials)
        self._save_credentials(record)
        return record

    # -- Sign-out --------------------------------------------------------------

    def logout(self) -> dict[str, Any]:
        """Revoke the renewable session, then forget the tokens; the registration stays."""
        with self._lock:
            self._stop_pending()
            credentials = self._credentials()
            confirmed = True
            if credentials.get("refresh_token"):
                try:
                    endpoint = str(self._openid().get("revocation_endpoint") or "")
                    response = httpx.post(endpoint, data={
                        "token": credentials["refresh_token"],
                        "token_type_hint": "refresh_token",
                        "client_id": credentials["client_id"],
                    }, timeout=_HTTP_TIMEOUT) if endpoint else None
                    confirmed = response is not None and response.status_code == 200
                except httpx.HTTPError:
                    confirmed = False
            self._clear_credentials()
            result = self.status()
            if not confirmed:
                result["login_error"] = (
                    "Signed out here, but ChatGPT did not confirm it. "
                    "You can disconnect Monaw in ChatGPT settings.")
            return result


def plan_error_from_response(status: int, code: str, message: str, param: str = "") -> ChatGPTAccountError | None:
    """Map a Responses API failure on the plan route to a user-facing error."""
    if code == "subscription_sharing_usage_limit_exceeded":
        return ChatGPTAccountError(
            "You've reached your ChatGPT plan's usage limit for Monaw. "
            "Check usage in ChatGPT under Settings > Usage, or switch to an API key.", code=code)
    if code == "subscription_sharing_user_not_eligible":
        return ChatGPTAccountError("This ChatGPT account can't use its plan in Monaw.", code=code)
    if code == "subscription_sharing_unsupported_capability":
        return ChatGPTAccountError(
            f"ChatGPT plan requests don't support {param or 'part of this request'}.", code=code)
    if code in {"subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable"} or (
            status == 503 and not code):
        return ChatGPTAccountError("ChatGPT plan usage is temporarily unavailable.", code=code, retryable=True)
    if code == "subscription_sharing_invalid_user" or status == 401:
        return ChatGPTAccountError("ChatGPT did not accept the sign-in. Sign in again in Settings.",
                                   code=code or "unauthorized")
    if code in {"subscription_sharing_route_not_supported", "chatpass_v2_scope_not_authorized",
                "chatpass_v2_invalid_authorization_context"} or status == 403:
        return ChatGPTAccountError(f"ChatGPT refused the request: {message or code or 'not permitted'}.",
                                   code=code or "forbidden")
    return None


chatgpt_account = ChatGPTAccountService()
