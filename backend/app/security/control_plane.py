from __future__ import annotations

import base64
import binascii
from collections import defaultdict, deque
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from typing import Iterable

from fastapi import HTTPException, Request, status


CONTROL_SECRET_ENV = "MONAW_CONTROL_SECRET"
ALLOW_DEVELOPMENT_TOKEN_ENV = "MONAW_ALLOW_DEVELOPMENT_TOKEN"
TOKEN_ISSUER = "monaw-electron"
ALL_SCOPES = frozenset(
    {
        "agent:run",
        "settings:read",
        "settings:write",
        "approval:resolve",
        "diagnostics:read",
        "diagnostics:control",
        "files:read",
    }
)
MAX_AUTH_FAILURES_PER_MINUTE = 120
_FAILURES: dict[str, deque[float]] = defaultdict(deque)
_FAILURE_LOCK = threading.Lock()


@dataclass(frozen=True)
class ControlSession:
    session_id: str
    scopes: frozenset[str]
    expires_at: int
    issuer: str


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")


def _control_secret() -> str:
    return os.environ.get(CONTROL_SECRET_ENV, "").strip()


def _required_scope(method: str, path: str) -> str:
    method = method.upper()
    if path.startswith("/api/settings"):
        return "settings:read" if method == "GET" else "settings:write"
    if path.startswith("/api/sandbox"):
        return "settings:read"
    if path.startswith("/api/approvals") or path.startswith("/api/access-grants"):
        return "approval:resolve"
    if path.startswith("/api/diagnostics") or path.startswith("/api/observability"):
        return "diagnostics:read" if method == "GET" else "diagnostics:control"
    if path.startswith("/api/files"):
        return "files:read"
    return "agent:run"


def mint_control_token(
    secret: str,
    *,
    scopes: Iterable[str] = ALL_SCOPES,
    lifetime_seconds: int = 300,
    now: int | None = None,
    session_id: str | None = None,
) -> str:
    issued_at = int(time.time() if now is None else now)
    payload = {
        "iss": TOKEN_ISSUER,
        "iat": issued_at,
        "exp": issued_at + max(1, int(lifetime_seconds)),
        "jti": session_id or secrets.token_urlsafe(18),
        "scopes": sorted(set(scopes)),
    }
    encoded = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64url_encode(signature)}"


def _record_failure(client_id: str) -> None:
    now = time.monotonic()
    with _FAILURE_LOCK:
        failures = _FAILURES[client_id]
        while failures and now - failures[0] > 60:
            failures.popleft()
        failures.append(now)
        if len(failures) > MAX_AUTH_FAILURES_PER_MINUTE:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many authentication failures",
                headers={"Retry-After": "60"},
            )


def _clear_failures(client_id: str) -> None:
    with _FAILURE_LOCK:
        _FAILURES.pop(client_id, None)


def _credentials_error(detail: str = "Invalid control-plane credentials") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def verify_control_token(token: str, secret: str, *, now: int | None = None) -> ControlSession:
    if (
        os.environ.get(ALLOW_DEVELOPMENT_TOKEN_ENV, "").strip() == "1"
        and hmac.compare_digest(token, secret)
    ):
        return ControlSession(
            session_id="development",
            scopes=ALL_SCOPES,
            expires_at=2**31 - 1,
            issuer="development",
        )

    try:
        encoded, supplied_signature = token.split(".", 1)
        expected_signature = hmac.new(
            secret.encode("utf-8"),
            encoded.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_b64url_decode(supplied_signature), expected_signature):
            raise ValueError("signature mismatch")
        payload = json.loads(_b64url_decode(encoded))
        if not isinstance(payload, dict):
            raise ValueError("invalid payload")
        issuer = str(payload.get("iss") or "")
        expires_at = int(payload.get("exp") or 0)
        issued_at = int(payload.get("iat") or 0)
        current_time = int(time.time() if now is None else now)
        if issuer != TOKEN_ISSUER:
            raise ValueError("invalid issuer")
        if issued_at > current_time + 30:
            raise ValueError("issued in the future")
        if expires_at <= current_time:
            raise ValueError("expired")
        raw_scopes = payload.get("scopes")
        if not isinstance(raw_scopes, list) or not all(isinstance(item, str) for item in raw_scopes):
            raise ValueError("invalid scopes")
        session_id = str(payload.get("jti") or "")
        if not session_id:
            raise ValueError("missing session id")
    except (ValueError, TypeError, binascii.Error, json.JSONDecodeError, UnicodeDecodeError):
        raise _credentials_error() from None

    return ControlSession(
        session_id=session_id,
        scopes=frozenset(raw_scopes),
        expires_at=expires_at,
        issuer=issuer,
    )


async def require_control_session(request: Request) -> ControlSession:
    secret = _control_secret()
    if len(secret) < 32:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Control-plane authentication is not configured",
        )

    client_id = request.client.host if request.client else "unknown"
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        _record_failure(client_id)
        raise _credentials_error("Missing control-plane credentials")

    try:
        session = verify_control_token(token.strip(), secret)
    except HTTPException:
        _record_failure(client_id)
        raise

    required_scope = _required_scope(request.method, request.url.path)
    if required_scope not in session.scopes:
        _record_failure(client_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing required scope: {required_scope}",
        )

    _clear_failures(client_id)
    request.state.control_session = session
    return session
