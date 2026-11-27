"""Single connector lifecycle shared by desktop, scheduled, and Telegram runs."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid

from app.agent.tool_registry import validate_tool_args

from .auth import AuthorizationSessions
from .models import Account, ConnectorError, ConnectorResult, Credentials, Identity, RefreshResult
from .providers import CallContext, ProviderRegistry
from .store import AccountStore
from .vault import CredentialVault


class ConnectorService:
    def __init__(self, store: AccountStore, providers: ProviderRegistry, vault: CredentialVault):
        """Stripe locks bound memory while serializing each account's lifecycle."""
        self.store = store
        self.providers = providers
        self.vault = vault
        self.authorizations = AuthorizationSessions()
        self._locks = [threading.RLock() for _ in range(64)]

    def _lock(self, account_id: str):
        """Use the same lock for dispatch, refresh, settings, and disconnect."""
        return self._locks[int(hashlib.sha256(account_id.encode()).hexdigest(), 16) % len(self._locks)]

    def fingerprint(self) -> str:
        """Invalidate tool discovery when account access or provider contracts change."""
        public = {"accounts": [a.model_dump() for a in self.store.list()],
                  "providers": [m.model_dump() for m in self.providers.manifests()]}
        return hashlib.sha256(json.dumps(public, sort_keys=True, default=sorted).encode()).hexdigest()

    def _capabilities(self, provider_id: str, selected: frozenset[str], scopes: frozenset[str]) -> None:
        """An application toggle can restrict provider grants but never expand them."""
        capabilities = {c.id: c for c in self.providers.get(provider_id).manifest.capabilities}
        if not selected.issubset(capabilities):
            raise ConnectorError("capability_disabled")
        if any(not capabilities[name].required_scopes.issubset(scopes) for name in selected):
            raise ConnectorError("scope_missing")

    def connect_verified(self, provider_id: str, identity: Identity, credentials: Credentials, *,
                         auth_method: str, enabled_capabilities: frozenset[str],
                         account_id: str | None = None, expected_revision: int | None = None) -> Account:
        """Trusted sign-in adapters call this after verifying identity and actual grants."""
        manifest = self.providers.get(provider_id).manifest
        if auth_method not in manifest.auth_methods:
            raise ConnectorError("authorization_invalid")
        self._credential_type(auth_method, credentials)
        if (account_id is None) != (expected_revision is None):
            raise ConnectorError("authorization_invalid")
        self._capabilities(provider_id, enabled_capabilities, identity.granted_scopes)
        identifier = account_id or uuid.uuid4().hex
        with self._lock(identifier):
            previous = self.store.get(identifier) if account_id else None
            if previous and (previous.revision != expected_revision or
                             (previous.provider_id, previous.subject, previous.tenant, previous.auth_method) !=
                             (provider_id, identity.subject, identity.tenant, auth_method)):
                raise ConnectorError("account_changed")
            now = time.time()
            account = Account(id=identifier, provider_id=provider_id, **identity.model_dump(),
                              auth_method=auth_method, enabled_capabilities=enabled_capabilities,
                              revision=previous.revision + 1 if previous else 1,
                              created_at=previous.created_at if previous else now, updated_at=now)
            encrypted = self.vault.seal(account, credentials)
            if previous:
                self.store.replace(account, previous.revision, encrypted=encrypted)
            else:
                self.store.insert(account, encrypted)
            return account

    def _credential_type(self, auth_method: str, credentials: Credentials) -> None:
        """Prevent adapters from confusing API keys, OAuth tokens, and anonymous access."""
        required = {"none": "none", "api_key": "api_key"}.get(auth_method, "Bearer")
        if credentials.token_type != required:
            raise ConnectorError("authorization_invalid")

    def update(self, account_id: str, *, expected_revision: int, enabled: bool,
               enabled_capabilities: frozenset[str], label: str) -> Account:
        """Apply a settings edit only to the exact revision the user inspected."""
        with self._lock(account_id):
            account = self.store.get(account_id)
            if account.revision != expected_revision:
                raise ConnectorError("account_changed")
            self._capabilities(account.provider_id, enabled_capabilities, account.granted_scopes)
            updated = Account.model_validate({**account.model_dump(), "enabled": enabled,
                "enabled_capabilities": enabled_capabilities, "label": label,
                "revision": account.revision + 1, "updated_at": time.time()})
            self.store.replace(updated, account.revision)
            return updated

    def disconnect(self, account_id: str, *, expected_revision: int) -> Account:
        """Remove local credentials atomically; keep identity so reconnect is explicit."""
        with self._lock(account_id):
            account = self.store.get(account_id)
            if account.revision != expected_revision:
                raise ConnectorError("account_changed")
            updated = account.model_copy(update={"state": "disconnected", "enabled": False,
                "enabled_capabilities": frozenset(), "revision": account.revision + 1, "updated_at": time.time()})
            self.store.replace(updated, account.revision, clear_credentials=True)
            return updated

    def remove(self, account_id: str, *, expected_revision: int) -> None:
        """Forget a disconnected or active account without requiring its provider online."""
        with self._lock(account_id):
            account = self.store.get(account_id)
            if account.revision != expected_revision:
                raise ConnectorError("account_changed")
            self.store.delete(account)

    def _credentials(self, account: Account, adapter, context: CallContext) -> tuple[Account, Credentials]:
        """Refresh once under the account lock, durably recording ambiguous interruptions."""
        credentials = self.vault.open(account, self.store.credentials(account))
        if credentials.expires_at is None or credentials.expires_at > time.time() + 60:
            return account, credentials
        if credentials.refresh_token is None:
            self._reauth(account)
            raise ConnectorError("needs_reauth")
        # A crash after provider rotation must not replay the old refresh token.
        refreshing = account.model_copy(update={"state": "refreshing", "revision": account.revision + 1,
                                                "updated_at": time.time()})
        self.store.replace(refreshing, account.revision)
        try:
            context.remaining()
            refresh = adapter.refresh(credentials, context)
            if not isinstance(refresh, RefreshResult):
                raise ConnectorError("needs_reauth")
            refreshed = refresh.credentials
            self._credential_type(account.auth_method, refreshed)
            if refreshed.expires_at is not None and refreshed.expires_at <= time.time() + 60:
                raise ConnectorError("needs_reauth")
            # OAuth providers may omit an unchanged refresh token in their reply.
            if refreshed.refresh_token is None:
                refreshed = refreshed.model_copy(update={"refresh_token": credentials.refresh_token})
            scopes = account.granted_scopes if refresh.granted_scopes is None else refresh.granted_scopes
            enabled = frozenset(c.id for c in adapter.manifest.capabilities
                                if c.id in account.enabled_capabilities and c.required_scopes.issubset(scopes))
            ready = refreshing.model_copy(update={"state": "ready", "revision": refreshing.revision + 1,
                "granted_scopes": scopes, "enabled_capabilities": enabled, "updated_at": time.time()})
            self.store.replace(ready, refreshing.revision, encrypted=self.vault.seal(ready, refreshed))
            return ready, refreshed
        except Exception:
            self._reauth(refreshing)
            raise ConnectorError("needs_reauth") from None

    def _reauth(self, account: Account) -> None:
        """Persist a closed credential state without resurrecting a competing edit."""
        changed = account.model_copy(update={"state": "needs_reauth", "revision": account.revision + 1,
                                             "updated_at": time.time()})
        self.store.replace(changed, account.revision, clear_credentials=True)

    def invoke(self, provider_id: str, capability_id: str, account_id: str, arguments: dict, *,
               expected_fingerprint: str, expected_revision: int | None = None,
               cancelled: threading.Event | None = None) -> dict:
        """Revalidate account, grants, schema, and policy immediately before dispatch."""
        from app.agent.action_security import attest_action, authorize_action
        from .tools import manifest_fingerprint, tool_name

        with self._lock(account_id):
            account = self.store.get(account_id)
            adapter = self.providers.get(provider_id)
            manifest = adapter.manifest
            if account.provider_id != provider_id or manifest_fingerprint(manifest) != expected_fingerprint:
                raise ConnectorError("account_changed")
            if expected_revision is not None and account.revision != expected_revision:
                raise ConnectorError("account_changed")
            if not account.enabled:
                raise ConnectorError("account_disabled")
            if account.state != "ready":
                raise ConnectorError("needs_reauth")
            if capability_id not in account.enabled_capabilities:
                raise ConnectorError("capability_disabled")
            self._capabilities(provider_id, frozenset({capability_id}), account.granted_scopes)
            capability = next(c for c in manifest.capabilities if c.id == capability_id)
            if not isinstance(arguments, dict) or validate_tool_args(arguments, capability.parameters):
                raise ConnectorError("invalid_arguments")
            try:
                encoded_arguments = json.dumps(arguments, allow_nan=False)
                if len(encoded_arguments.encode()) > 256 * 1024:
                    raise ValueError("Arguments too large")
                arguments = json.loads(encoded_arguments)
            except (TypeError, ValueError):
                raise ConnectorError("invalid_arguments") from None
            context = CallContext(account=account, deadline=time.monotonic() + 45,
                                  cancelled=cancelled or threading.Event())
            context.remaining()
            account, credentials = self._credentials(account, adapter, context)
            if capability_id not in account.enabled_capabilities:
                raise ConnectorError("scope_missing")
            effect = "none" if capability.action == "read" else "external_mutation"
            payload = {"account_id": account.id, "arguments": arguments,
                       "expected_revision": account.revision, "expected_fingerprint": expected_fingerprint}
            describe = getattr(adapter, "action_target", None)
            target_details = describe(capability, arguments, account) if describe else {}
            descriptor = attest_action(
                tool_name=tool_name(provider_id, capability_id), action_kind=capability.action,
                parameters={"account_id": account.id, "arguments": arguments},
                target={"app": manifest.name, "provider": provider_id, "account_id": account.id, "subject": account.subject,
                        "tenant": account.tenant, "capability": capability_id},
                destination=target_details.get("destination", f"connector://{provider_id}/{account.id}"), side_effect=effect,
                context_version=f"{account.revision}:{expected_fingerprint}",
                data_scope={"granted_scopes": sorted(account.granted_scopes)},
                runtime_facts={"target_verified": True, "context_verified": True,
                               "connector_preview": target_details.get("preview", {}) if effect != "none" else {}},
            )
            decision = authorize_action(descriptor, replay_payload=payload)
            if decision is not None:
                return decision
            if self.store.get(account.id).revision != account.revision:
                raise ConnectorError("account_changed")
            context = CallContext(account=account, deadline=context.deadline, cancelled=context.cancelled)
            context.remaining()
            try:
                result = adapter.invoke(capability, arguments, credentials, context)
                if not isinstance(result, ConnectorResult):
                    raise ConnectorError("invalid_result", uncertain=effect != "none")
                output = result.model_dump(mode="json")
                encoded = json.dumps(output, allow_nan=False)
                if len(encoded.encode()) > 256 * 1024:
                    raise ConnectorError("invalid_result", uncertain=effect != "none")
                from app.agent.action_security.sensitive_data import redact_sensitive

                return {"status": "ok", "account_id": account.id, "content_trust": "untrusted",
                        **redact_sensitive(output)}
            except ConnectorError as exc:
                if exc.code == "needs_reauth":
                    self._reauth(account)
                if effect != "none" and exc.code in {"timeout", "cancelled", "provider_error", "invalid_result"}:
                    exc.uncertain = True
                raise
            except Exception:
                # A lost response never proves that a remote write did not happen.
                raise ConnectorError("provider_error", uncertain=effect != "none") from None


_service: ConnectorService | None = None
_service_lock = threading.Lock()


def get_connector_service() -> ConnectorService:
    """Lazily construct the shared service without provider network calls at startup."""
    from app.agent.database import get_db

    global _service
    with _service_lock:
        database = get_db()
        if _service is None or _service.store.db is not database:
            _service = ConnectorService(AccountStore(database), ProviderRegistry(), CredentialVault())
            from .github import GitHubProvider
            from .github_auth import GitHubSignIn

            _service.providers.register(GitHubProvider())
            _service.github_sign_in = GitHubSignIn(_service)
            from .composio import ComposioAccounts, ComposioProvider
            from .composio_catalog import MANIFESTS

            _service.composio = ComposioAccounts(_service)
            for manifest in MANIFESTS:
                _service.providers.register(ComposioProvider(manifest, _service.composio))
        return _service
