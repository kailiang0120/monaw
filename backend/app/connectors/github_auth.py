"""Bounded GitHub device authorization; no token ever returns to the renderer."""

from __future__ import annotations

import secrets
import math
import threading
import time
from dataclasses import dataclass, field

from . import github
from .models import ConnectorError, Credentials


@dataclass
class Login:
    owner: str
    client_id: str
    device_code: str = field(repr=False)
    expires_at: float
    interval: float
    next_poll: float
    account_id: str | None
    revision: int | None
    allow_writes: bool
    polling: bool = False


def enabled_capabilities(allow_writes: bool) -> frozenset[str]:
    """Default new connections to read capabilities; writes require an explicit choice."""
    return frozenset(c.id for c in github.MANIFEST.capabilities if allow_writes or c.action == "read")


class GitHubSignIn:
    def __init__(self, service):
        """Keep device secrets in a bounded backend-only collection."""
        self.service = service
        self._pending: dict[str, Login] = {}
        self._lock = threading.RLock()

    def _check_reconnect(self, account_id, revision, method):
        """Reject stale reconnect forms before starting an external sign-in."""
        if (account_id is None) != (revision is None):
            raise ConnectorError("authorization_invalid")
        if account_id:
            account = self.service.store.get(account_id)
            if (
                account.provider_id != "github"
                or account.auth_method != method
                or account.revision != revision
            ):
                raise ConnectorError("account_changed")

    def connect_token(self, token: str, *, account_id=None, revision=None, allow_writes=False) -> dict:
        """Verify a user-entered PAT through GitHub before storing it encrypted."""
        self._check_reconnect(account_id, revision, "api_key")
        credentials = Credentials(access_token=token, token_type="api_key")
        identity = github.verified_identity(token)
        account = self.service.connect_verified(
            "github",
            identity,
            credentials,
            auth_method="api_key",
            enabled_capabilities=enabled_capabilities(allow_writes),
            account_id=account_id,
            expected_revision=revision,
        )
        return account.model_dump(mode="json")

    def start(self, owner, *, account_id=None, revision=None, allow_writes=False) -> dict:
        """Return only the public verification code and an opaque local login ID."""
        self._check_reconnect(account_id, revision, "device_code")
        config = github.github_configuration()
        if not config["client_id"]:
            raise ConnectorError("provider_unavailable")
        with self._lock:
            self._pending = {
                key: login for key, login in self._pending.items() if login.expires_at > time.monotonic()
            }
            if len(self._pending) >= 8:
                raise ConnectorError("rate_limited", retry_after=60)
            data, _ = github.request_json(
                "/login/device/code", oauth=True, method="POST", data={"client_id": config["client_id"]}
            )
            if not isinstance(data, dict) or not all(
                isinstance(data.get(key), str) and 0 < len(data[key]) <= 1000
                for key in ("device_code", "user_code")
            ):
                raise ConnectorError("authorization_invalid")
            from app.security.mcp_credentials import remember_secrets

            remember_secrets([data["device_code"]])
            try:
                interval = float(data.get("interval", 5))
                lifetime = float(data["expires_in"])
                if not math.isfinite(interval) or not math.isfinite(lifetime) or lifetime <= 0:
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise ConnectorError("authorization_invalid") from None
            interval = max(5, interval)
            lifetime = min(900, lifetime)
            login_id = secrets.token_urlsafe(32)
            self._pending[login_id] = Login(
                owner,
                config["client_id"],
                data["device_code"],
                time.monotonic() + lifetime,
                interval,
                time.monotonic() + interval,
                account_id,
                revision,
                allow_writes,
            )
            return {
                "login_id": login_id,
                "user_code": str(data["user_code"]),
                "expires_in": lifetime,
                "interval": interval,
                "verification_uri": "https://github.com/login/device",
            }

    def poll(self, login_id, owner) -> dict:
        """Respect GitHub polling intervals and bind completion to the initiating session."""
        with self._lock:
            login = self._pending.get(login_id)
            if login is None or login.owner != owner:
                raise ConnectorError("authorization_invalid")
            if login.expires_at <= time.monotonic():
                del self._pending[login_id]
                raise ConnectorError("authorization_expired")
            if login.polling or login.next_poll > time.monotonic():
                return {"status": "pending", "interval": max(1, login.next_poll - time.monotonic())}
            login.polling = True
            login.next_poll = time.monotonic() + login.interval
        # Do not hold the collection lock across network calls: Cancel must win
        # while GitHub is slow, before any new credentials are persisted.
        try:
            data, _ = github.request_json(
                "/login/oauth/access_token",
                oauth=True,
                method="POST",
                data={
                    "client_id": login.client_id,
                    "device_code": login.device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
            if not isinstance(data, dict):
                raise ConnectorError("authorization_invalid")
            error = data.get("error")
            if error in {"authorization_pending", "slow_down"}:
                with self._lock:
                    if self._pending.get(login_id) is not login:
                        raise ConnectorError("authorization_invalid")
                    if error == "slow_down":
                        login.interval += 5
                    login.next_poll = time.monotonic() + login.interval
                    login.polling = False
                    return {"status": "pending", "interval": login.interval}
            if error:
                raise ConnectorError(
                    "authorization_expired" if error == "expired_token" else "authorization_invalid"
                )
            credentials = github.token_credentials(data, login.client_id)
            identity = github.verified_identity(credentials.access_token.get_secret_value())
            with self._lock:
                if self._pending.get(login_id) is not login:
                    raise ConnectorError("authorization_invalid")
                if login.expires_at <= time.monotonic():
                    raise ConnectorError("authorization_expired")
                # Consume before committing, even if credential persistence fails.
                del self._pending[login_id]
                account = self.service.connect_verified(
                    "github",
                    identity,
                    credentials,
                    auth_method="device_code",
                    enabled_capabilities=enabled_capabilities(login.allow_writes),
                    account_id=login.account_id,
                    expected_revision=login.revision,
                )
                return {"status": "connected", "account": account.model_dump(mode="json")}
        except Exception:
            with self._lock:
                self._pending.pop(login_id, None)
            raise

    def cancel(self, login_id, owner) -> dict:
        """Discard only this user's attempt without revoking another account."""
        with self._lock:
            login = self._pending.get(login_id)
            if login is not None and login.owner == owner:
                del self._pending[login_id]
            return {"cancelled": True}
