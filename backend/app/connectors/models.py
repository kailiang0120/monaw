"""Versioned connector contracts; public metadata never contains credentials."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, model_validator

Identifier = Annotated[
    str, Field(max_length=32, pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
]
AccountId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
AuthMethod = Literal["oauth_pkce", "device_code", "api_key", "none"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Capability(Contract):
    id: Identifier
    description: str = Field(min_length=1, max_length=2000)
    parameters: dict[str, Any]
    action: Literal["read", "create", "update", "delete", "send_message", "upload"]
    required_scopes: frozenset[str] = frozenset()


class ProviderManifest(Contract):
    contract_version: Literal[1] = 1
    id: Identifier
    name: str = Field(min_length=1, max_length=80)
    category: Literal[
        "email",
        "calendar",
        "files",
        "knowledge",
        "messaging",
        "tasks",
        "development",
        "other",
    ] = "other"
    description: str = Field(default="", max_length=240)
    connection_method: Literal["native", "composio"] = "native"
    setup_notice: str = Field(default="", max_length=600)
    version: str = Field(min_length=1, max_length=64)
    transport: Literal["api", "mcp"] = "api"
    auth_methods: frozenset[AuthMethod]
    capabilities: tuple[Capability, ...]

    @model_validator(mode="after")
    def unique_capabilities(self):
        """Reject ambiguous manifests before any account can use them."""
        if not self.auth_methods or len({c.id for c in self.capabilities}) != len(
            self.capabilities
        ):
            raise ValueError(
                "Auth methods and unique capability identifiers are required"
            )
        return self


class Account(Contract):
    id: AccountId
    provider_id: Identifier
    subject: str = Field(min_length=1, max_length=512)
    tenant: str = Field(default="", max_length=512)
    label: str = Field(min_length=1, max_length=200)
    auth_method: AuthMethod
    granted_scopes: frozenset[str] = frozenset()
    enabled_capabilities: frozenset[str] = frozenset()
    enabled: bool = True
    revision: int = Field(default=1, ge=1)
    state: Literal["ready", "refreshing", "needs_reauth", "disconnected"] = "ready"
    created_at: float
    updated_at: float


class GatewayBinding(Contract):
    """Encrypted routing metadata for an explicitly imported runtime connection."""

    origin: str = Field(min_length=1, max_length=2048)
    connection_id: str = Field(
        min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_-]+$"
    )
    auth_config_id: str | None = Field(
        default=None, max_length=256, pattern=r"^[A-Za-z0-9_-]+$"
    )


class Credentials(Contract):
    """Only trusted adapters and the encrypted store receive this object."""

    access_token: SecretStr
    refresh_token: SecretStr | None = None
    expires_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    token_type: Literal["Bearer", "api_key", "none"] = "Bearer"
    client_id: str | None = Field(default=None, max_length=128)
    gateway: GatewayBinding | None = None

    @model_validator(mode="after")
    def bounded_values(self):
        """Reject empty, oversized, or placeholder secrets before persistence."""
        from app.security.mcp_credentials import validate_secret

        if self.token_type != "none":
            validate_secret(self.access_token.get_secret_value())
        if self.refresh_token is not None:
            validate_secret(self.refresh_token.get_secret_value())
        return self


class Identity(Contract):
    """Identity and grants verified by the provider, never taken from chat input."""

    subject: str = Field(min_length=1, max_length=512)
    tenant: str = Field(default="", max_length=512)
    label: str = Field(min_length=1, max_length=200)
    granted_scopes: frozenset[str] = frozenset()


class RefreshResult(Contract):
    credentials: Credentials
    # None means the provider did not return a changed grant; empty means no grants.
    granted_scopes: frozenset[str] | None = None


class ConnectorResult(Contract):
    """Bounded page of provider data; adapters keep downloads out of JSON."""

    data: Any = None
    next_cursor: str | None = Field(default=None, max_length=8192)
    links: tuple[HttpUrl, ...] = Field(default=(), max_length=20)


class ConnectorError(Exception):
    """Safe machine-readable failures, without provider response bodies."""

    def __init__(
        self, code: str, *, retry_after: float | None = None, uncertain: bool = False
    ):
        """Keep only known public codes; never propagate arbitrary SDK text."""
        allowed = {
            "provider_unavailable",
            "account_not_found",
            "account_changed",
            "account_disabled",
            "account_exists",
            "capability_disabled",
            "scope_missing",
            "needs_reauth",
            "credentials_unavailable",
            "invalid_arguments",
            "rate_limited",
            "provider_error",
            "timeout",
            "cancelled",
            "invalid_result",
            "authorization_expired",
            "authorization_invalid",
        }
        self.code = code if code in allowed else "provider_error"
        self.retry_after = (
            min(86400.0, max(0.0, retry_after)) if retry_after is not None else None
        )
        self.uncertain = uncertain
        super().__init__(self.code)

    def payload(self) -> dict:
        """Expose recovery information without secrets or raw exception strings."""
        return {
            "status": "error",
            "code": self.code,
            "retry_after": self.retry_after,
            "outcome_uncertain": self.uncertain,
        }
