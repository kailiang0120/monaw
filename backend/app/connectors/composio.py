"""User-owned Composio connections behind Monaw's existing permission contracts."""

import secrets
import threading
import time
from dataclasses import dataclass, field

from pydantic import SecretStr

from app.security.mcp_credentials import remember_secrets, validate_secret

from .composio_gateway import ComposioGateway, ORIGIN
from .composio_catalog import MANIFEST_BY_ID
from .models import (
    ConnectorError,
    ConnectorResult,
    Credentials,
    GatewayBinding,
    Identity,
)


@dataclass(frozen=True)
class Configuration:
    user: str
    generation: str
    key: str = field(repr=False)


@dataclass
class Login:
    owner: str
    provider: str
    generation: str
    session: str
    connection: str
    auth_config: str
    expires: float
    next_poll: float
    account_id: str | None
    revision: int | None
    polling: bool = False


class ComposioAccounts:
    def __init__(self, service, gateway=None):
        """Keep secrets in OS-encrypted SQLite and linking attempts in bounded memory."""
        self.service = service
        self.gateway = gateway or ComposioGateway()
        self.lock = threading.RLock()
        self.pending = {}

    def configuration(self):
        """Return availability only; no credentials or remote calls during startup."""
        row = self.service.store.db.fetchone(
            "SELECT key_blob FROM composio_configuration WHERE id = 1"
        )
        return {"configured": bool(row and row["key_blob"])}

    def current(self):
        """Decrypt internally and fail closed if OS protection or configuration is absent."""
        row = self.service.store.db.fetchone(
            "SELECT * FROM composio_configuration WHERE id = 1"
        )
        if not row or not row["key_blob"]:
            raise ConnectorError("provider_unavailable")
        try:
            key = self.service.vault.cipher.decrypt(
                bytes(row["key_blob"]), b"monaw-composio-v1"
            ).decode()
            validate_secret(key)
        except Exception:
            raise ConnectorError("credentials_unavailable") from None
        remember_secrets([key])
        return Configuration(row["user_id"], row["generation"], key)

    def save_key(self, key):
        """Validate before replacing a working key, and verify the encrypted round trip."""
        try:
            key = validate_secret(key.strip())
            if len(key) > 4096 or any(
                ord(char) <= 32 or ord(char) > 126 for char in key
            ):
                raise ValueError()
        except ValueError:
            raise ConnectorError("authorization_invalid") from None
        remember_secrets([key])
        self.gateway.validate(key)
        try:
            encrypted = self.service.vault.cipher.encrypt(
                key.encode(), b"monaw-composio-v1"
            )
            if (
                self.service.vault.cipher.decrypt(encrypted, b"monaw-composio-v1")
                != key.encode()
            ):
                raise ValueError()
        except Exception:
            raise ConnectorError("credentials_unavailable") from None
        with self.lock:
            if self.configuration()["configured"] and secrets.compare_digest(
                self.current().key, key
            ):
                return self.configuration()
            row = self.service.store.db.fetchone(
                "SELECT user_id FROM composio_configuration WHERE id = 1"
            )
            user = row["user_id"] if row else "monaw_" + secrets.token_hex(16)
            generation = secrets.token_hex(16)
            with self.service.store.db.transaction() as connection:
                connection.execute(
                    "INSERT INTO composio_configuration VALUES (1, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET generation=excluded.generation, key_blob=excluded.key_blob",
                    (user, generation, encrypted),
                )
            self.pending.clear()
            self._disconnect_accounts()
        return self.configuration()

    def _disconnect_accounts(self):
        """Invalidate saved bindings while the caller holds the configuration lock."""
        for account in self.service.store.list():
            if (
                account.provider_id in MANIFEST_BY_ID
                and account.state != "disconnected"
            ):
                try:
                    self.service.disconnect(
                        account.id, expected_revision=account.revision
                    )
                except ConnectorError as error:
                    if error.code != "account_changed":
                        raise

    def remove_key(self):
        """Stop local access immediately, retaining no key snapshot in account credentials."""
        with self.lock:
            with self.service.store.db.transaction() as connection:
                connection.execute(
                    "UPDATE composio_configuration SET key_blob=NULL, generation=? WHERE id=1",
                    (secrets.token_hex(16),),
                )
            self.pending.clear()
            self._disconnect_accounts()
        return self.configuration()

    def start(self, owner, provider_id, account_id=None, expected_revision=None):
        """Open a session-owned Connect Link; configuration is provisioned by Composio."""
        manifest = self.service.providers.get(provider_id).manifest
        if manifest.setup_notice:
            raise ConnectorError("provider_unavailable")
        if provider_id not in MANIFEST_BY_ID or (account_id is None) != (
            expected_revision is None
        ):
            raise ConnectorError("authorization_invalid")
        if account_id:
            account = self.service.store.get(account_id)
            if (
                account.provider_id != provider_id
                or account.revision != expected_revision
            ):
                raise ConnectorError("account_changed")
        with self.lock:
            self.pending = {
                key: value
                for key, value in self.pending.items()
                if value.expires > time.monotonic()
            }
            if len(self.pending) >= 8:
                raise ConnectorError("rate_limited", retry_after=60)
            config = self.current()
            if account_id and account.tenant != "composio:" + config.user:
                raise ConnectorError("account_changed")
            saved = self.service.store.db.fetchone(
                "SELECT config_id FROM composio_auth_configs WHERE generation=? AND provider=?",
                (config.generation, provider_id),
            )
            auth_config = (
                saved["config_id"]
                if saved
                else self.gateway.auth_config(config.key, provider_id)
            )
            if not saved:
                with self.service.store.db.transaction() as connection:
                    connection.execute(
                        "INSERT INTO composio_auth_configs VALUES (?, ?, ?)",
                        (config.generation, provider_id, auth_config),
                    )
            session = self.gateway.session(
                config.key, config.user, provider_id, auth_config=auth_config
            )
            try:
                remote, url = self.gateway.link(config.key, session, provider_id)
            except ConnectorError:
                self.gateway.delete_session(config.key, session)
                raise
            login_id = secrets.token_urlsafe(32)
            self.pending[login_id] = Login(
                owner,
                provider_id,
                config.generation,
                session,
                remote,
                auth_config,
                time.monotonic() + 600,
                time.monotonic() + 3,
                account_id,
                expected_revision,
            )
        return {
            "login_id": login_id,
            "authorization_url": url,
            "interval": 3,
            "expires_in": 600,
        }

    def poll(self, login_id, owner):
        """Verify owner, active connection, and mailbox identity before committing exactly once."""
        with self.lock:
            login = self.pending.get(login_id)
            if not login or login.owner != owner:
                raise ConnectorError("authorization_invalid")
            if login.expires <= time.monotonic():
                self.pending.pop(login_id)
                raise ConnectorError("authorization_expired")
            if login.polling or login.next_poll > time.monotonic():
                return {"status": "pending", "interval": 3}
            config = self.current()
            if config.generation != login.generation:
                raise ConnectorError("account_changed")
            login.polling = True
            login.next_poll = time.monotonic() + 3
        session = None
        try:
            row = self.gateway.account(
                config.key, config.user, login.provider, login.connection
            )
            if row["auth_config"] != login.auth_config:
                raise ConnectorError("account_changed")
            if row["status"] in {"INITIALIZING", "INITIATED"}:
                return {"status": "pending", "interval": 3}
            if row["status"] != "ACTIVE":
                raise ConnectorError("needs_reauth")
            session = self.gateway.session(
                config.key,
                config.user,
                login.provider,
                login.connection,
                row["auth_config"],
            )
            data = self.gateway.execute(
                config.key, session, login.provider, "get_profile", {}
            )
            subject, label = profile(login.provider, data)
            with self.lock:
                if (
                    self.pending.get(login_id) is not login
                    or login.expires <= time.monotonic()
                    or self.current().generation != config.generation
                ):
                    raise ConnectorError("authorization_expired")
                identity = Identity(
                    subject=subject,
                    label=label,
                    tenant="composio:" + config.user,
                    granted_scopes={"composio:bound"},
                )
                credentials = Credentials(
                    access_token=SecretStr(secrets.token_urlsafe(32)),
                    token_type="api_key",
                    client_id=config.generation,
                    gateway=GatewayBinding(
                        origin=ORIGIN,
                        connection_id=login.connection,
                        auth_config_id=login.auth_config,
                    ),
                )
                manifest = self.service.providers.get(login.provider).manifest
                self.service.connect_verified(
                    login.provider,
                    identity,
                    credentials,
                    auth_method="api_key",
                    enabled_capabilities=frozenset(
                        cap.id for cap in manifest.capabilities
                    ),
                    account_id=login.account_id,
                    expected_revision=login.revision,
                )
                del self.pending[login_id]
            self.gateway.delete_session(config.key, login.session)
            return {"status": "connected"}
        except ConnectorError as error:
            with self.lock:
                if error.code == "rate_limited":
                    login.next_poll = time.monotonic() + 30
                    return {"status": "pending", "interval": 30}
                self.pending.pop(login_id, None)
            self.gateway.delete_session(config.key, login.session)
            raise
        finally:
            if session:
                self.gateway.delete_session(config.key, session)
            with self.lock:
                login.polling = False

    def cancel(self, login_id, owner):
        """Invalidate local completion first; never revoke a grant without user instruction."""
        with self.lock:
            login = self.pending.get(login_id)
            if login and login.owner == owner:
                del self.pending[login_id]
            else:
                login = None
        if login:
            try:
                config = self.current()
                if config.generation == login.generation:
                    self.gateway.delete_session(config.key, login.session)
            except ConnectorError:
                pass
        return {"cancelled": True}


def payload_data(data):
    """Unwrap only known execution envelopes, rejecting explicit provider failures."""
    for _ in range(3):
        if not isinstance(data, dict):
            raise ConnectorError("invalid_result")
        if data.get("successful") is False or data.get("ok") is False:
            raise ConnectorError("provider_error")
        if isinstance(data.get("response_data"), dict):
            data = data["response_data"]
        elif "successful" in data and isinstance(data.get("data"), dict):
            data = data["data"]
        else:
            return data
    raise ConnectorError("invalid_result")


def profile(provider, data):
    """Bind authenticated provider identities; workspace IDs prevent cross-tenant reconnects."""
    data = payload_data(data)
    if provider == "gmail":
        subject = label = data.get("emailAddress")
    elif provider in {"outlook", "one_drive", "microsoft_teams"}:
        subject = data.get("id")
        label = data.get("mail") or data.get("userPrincipalName")
    elif provider == "googlecalendar":
        subject = data.get("account_id") or data.get("email")
        label = data.get("email") or data.get("display_name") or subject
    elif provider == "googledrive":
        user = data.get("user", {})
        if not isinstance(user, dict):
            raise ConnectorError("invalid_result")
        subject = user.get("permissionId")
        label = user.get("emailAddress") or user.get("displayName")
    elif provider == "slack":
        team, user = data.get("team_id"), data.get("user_id")
        subject = f"{team}:{user}" if team and user else None
        label = f"{data.get('user') or user} - {data.get('team') or team}"
    elif provider == "notion":
        bot = data.get("bot") or {}
        if not isinstance(bot, dict):
            raise ConnectorError("invalid_result")
        subject = data.get("id")
        label = bot.get("workspace_name") or data.get("name")
    elif provider == "todoist":
        subject = str(data["id"]) if isinstance(data.get("id"), (str, int)) else None
        label = data.get("email") or data.get("full_name")
    elif provider == "linear":
        user = data.get("user", {})
        if not isinstance(user, dict):
            raise ConnectorError("invalid_result")
        subject, label = user.get("id"), user.get("email") or user.get("name")
    else:
        raise ConnectorError("provider_unavailable")
    if (
        not isinstance(subject, str)
        or not subject
        or not isinstance(label, str)
        or not label
    ):
        raise ConnectorError("invalid_result")
    if len(subject) > 512 or len(label) > 200:
        raise ConnectorError("invalid_result")
    return subject, label


def next_cursor(payload):
    """Extract bounded continuation tokens without consuming subsequent pages automatically."""
    metadata = payload.get("response_metadata") or {}
    page = payload.get("page_info") or {}
    if not isinstance(metadata, dict) or not isinstance(page, dict):
        raise ConnectorError("invalid_result")
    cursor = (
        payload.get("nextPageToken")
        or payload.get("next_page_token")
        or payload.get("@odata.nextLink")
        or payload.get("next_cursor")
        or metadata.get("next_cursor")
    )
    if not cursor and page.get("hasNextPage"):
        cursor = page.get("endCursor")
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 8192):
        raise ConnectorError("invalid_result")
    return cursor or None


class ComposioProvider:
    def __init__(self, manifest, accounts):
        """Share user-owned project configuration while preserving provider-specific manifests."""
        self.manifest = manifest
        self.accounts = accounts

    def invoke(self, capability, arguments, credentials, context):
        """Recheck the exact binding and identity before each read-only operation."""
        config = self.accounts.current()
        binding = credentials.gateway
        if (
            not binding
            or binding.origin != ORIGIN
            or credentials.client_id != config.generation
            or context.account.tenant != "composio:" + config.user
        ):
            raise ConnectorError("needs_reauth")
        gateway = self.accounts.gateway
        row = gateway.account(
            config.key,
            config.user,
            self.manifest.id,
            binding.connection_id,
            context.remaining,
        )
        if row["status"] != "ACTIVE":
            raise ConnectorError("needs_reauth")
        if row["auth_config"] != binding.auth_config_id:
            raise ConnectorError("account_changed")
        session = gateway.session(
            config.key,
            config.user,
            self.manifest.id,
            binding.connection_id,
            row["auth_config"],
            context.remaining,
        )
        try:
            data = gateway.execute(
                config.key,
                session,
                self.manifest.id,
                "get_profile",
                {},
                context.remaining,
            )
            if profile(self.manifest.id, data)[0] != context.account.subject:
                raise ConnectorError("account_changed")
            if self.accounts.current().generation != config.generation:
                raise ConnectorError("needs_reauth")
            if capability.id != "get_profile":
                data = gateway.execute(
                    config.key,
                    session,
                    self.manifest.id,
                    capability.id,
                    arguments,
                    context.remaining,
                )
            if capability.id == "get_profile":
                # Some upstream profiles contain tokens; expose only the verified identity.
                subject, label = profile(self.manifest.id, data)
                return ConnectorResult(data={"subject": subject, "label": label})
            payload = payload_data(data)
            return ConnectorResult(data=payload, next_cursor=next_cursor(payload))
        finally:
            gateway.delete_session(config.key, session)
