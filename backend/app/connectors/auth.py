"""Ephemeral authorization correlation for future provider sign-in adapters."""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field

from .models import ConnectorError


@dataclass(frozen=True)
class AuthorizationAttempt:
    state: str
    provider_id: str
    owner_session: str
    issuer: str
    redirect_uri: str
    scopes: frozenset[str]
    account_id: str | None
    account_revision: int | None
    expires_at: float
    verifier: str = field(repr=False)

    @property
    def challenge(self) -> str:
        """Return an RFC 7636 S256 challenge without exposing the verifier."""
        return base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode("ascii")).digest()).rstrip(b"=").decode()


class AuthorizationSessions:
    def __init__(self, *, clock=time.monotonic):
        """Keep short-lived login secrets in memory, never in SQLite or settings."""
        self._clock = clock
        self._attempts: dict[str, AuthorizationAttempt] = {}
        self._lock = threading.Lock()

    def begin(self, *, provider_id: str, owner_session: str, issuer: str, redirect_uri: str,
              scopes: frozenset[str], account_id: str | None = None,
              account_revision: int | None = None) -> AuthorizationAttempt:
        """Bind a login to trusted adapter endpoints and an optional account revision."""
        if not all((provider_id, owner_session, issuer, redirect_uri)):
            raise ConnectorError("authorization_invalid")
        if (account_id is None) != (account_revision is None):
            raise ConnectorError("authorization_invalid")
        with self._lock:
            self._attempts = {key: value for key, value in self._attempts.items()
                              if value.expires_at > self._clock()}
            if len(self._attempts) >= 8:
                raise ConnectorError("rate_limited", retry_after=60)
            attempt = AuthorizationAttempt(
                state=secrets.token_urlsafe(32), provider_id=provider_id, owner_session=owner_session,
                issuer=issuer, redirect_uri=redirect_uri, scopes=frozenset(scopes),
                account_id=account_id, account_revision=account_revision,
                expires_at=self._clock() + 600, verifier=secrets.token_urlsafe(48),
            )
            self._attempts[attempt.state] = attempt
            return attempt

    def consume(self, state: str, *, provider_id: str, owner_session: str,
                issuer: str, redirect_uri: str) -> AuthorizationAttempt:
        """Consume exactly once after validating all callback correlation fields."""
        with self._lock:
            attempt = self._attempts.get(state)
            if attempt is None:
                raise ConnectorError("authorization_invalid")
            if attempt.expires_at <= self._clock():
                del self._attempts[state]
                raise ConnectorError("authorization_expired")
            if (attempt.provider_id, attempt.owner_session, attempt.issuer, attempt.redirect_uri) != (
                    provider_id, owner_session, issuer, redirect_uri):
                raise ConnectorError("authorization_invalid")
            del self._attempts[state]
            return attempt

    def cancel(self, state: str, *, owner_session: str) -> None:
        """Only the initiating session can discard its pending sign-in."""
        with self._lock:
            attempt = self._attempts.get(state)
            if attempt is not None and attempt.owner_session == owner_session:
                del self._attempts[state]
