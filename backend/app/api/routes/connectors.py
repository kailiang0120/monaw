"""Public connector management; credentials and provider execution stay private."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, SecretStr

from app.connectors.models import AccountId, ConnectorError, Contract, Identifier
from app.connectors.service import get_connector_service

router = APIRouter(prefix="/connectors", tags=["connectors"])


class AccountUpdate(Contract):
    expected_revision: int = Field(ge=1)
    label: str = Field(min_length=1, max_length=200)
    enabled: bool
    enabled_capabilities: frozenset[Identifier]


class DisconnectRequest(Contract):
    expected_revision: int = Field(ge=1)


class ComposioKey(Contract):
    api_key: SecretStr = Field(min_length=1, max_length=4096)


class ComposioLogin(Contract):
    provider_id: Identifier
    account_id: AccountId | None = None
    expected_revision: int | None = Field(default=None, ge=1)


@router.get("/composio/configuration")
def composio_configuration() -> dict:
    """Expose configuration availability without the stored API key."""
    return get_connector_service().composio.configuration()


@router.put("/composio/key")
def composio_key(body: ComposioKey) -> dict:
    """Accept credentials only through the privileged desktop handoff."""
    try:
        return get_connector_service().composio.save_key(
            body.api_key.get_secret_value()
        )
    except ConnectorError as exc:
        raise _http_error(exc) from None


@router.delete("/composio/key")
def composio_remove_key() -> dict:
    """Forget the encrypted key and disable its local accounts."""
    try:
        return get_connector_service().composio.remove_key()
    except ConnectorError as exc:
        raise _http_error(exc) from None


@router.post("/composio/login")
def composio_login(body: ComposioLogin, request: Request) -> dict:
    """Create managed OAuth configuration and a session-owned Connect Link."""
    try:
        return get_connector_service().composio.start(
            request.state.control_session.session_id, **body.model_dump()
        )
    except ConnectorError as exc:
        raise _http_error(exc) from None


@router.post("/composio/login/{login_id}/poll")
def composio_poll(login_id: str, request: Request) -> dict:
    """Verify the exact account and mailbox before enabling local tools."""
    try:
        return get_connector_service().composio.poll(
            login_id, request.state.control_session.session_id
        )
    except ConnectorError as exc:
        raise _http_error(exc) from None


@router.delete("/composio/login/{login_id}")
def composio_cancel(login_id: str, request: Request) -> dict:
    """Cancel local linking; provider grants remain owned by the user."""
    return get_connector_service().composio.cancel(
        login_id, request.state.control_session.session_id
    )


def _http_error(error: ConnectorError) -> HTTPException:
    """Map safe connector failures into the existing API error envelope."""
    status = {
        "account_not_found": 404,
        "account_exists": 409,
        "account_changed": 409,
        "provider_unavailable": 409,
        "authorization_invalid": 400,
        "authorization_expired": 410,
        "needs_reauth": 400,
        "rate_limited": 429,
        "scope_missing": 403,
        "capability_disabled": 422,
    }.get(error.code, 503)
    return HTTPException(
        status, detail={"code": error.code, "message": error.code.replace("_", " ")}
    )


class GitHubConnect(Contract):
    account_id: AccountId | None = None
    revision: int | None = Field(default=None, ge=1)
    allow_writes: bool = False


class GitHubToken(GitHubConnect):
    token: SecretStr = Field(min_length=1, max_length=20000)


@router.get("/github/configuration")
def github_config() -> dict:
    """Expose only whether browser sign-in is configured and the public install link."""
    from app.connectors.github import github_configuration

    try:
        config = github_configuration()
        return {
            "device_login_available": bool(config["client_id"]),
            "installation_url": config["installation_url"],
        }
    except ConnectorError as exc:
        raise _http_error(exc) from None


@router.post("/github/token")
def github_token(body: GitHubToken) -> dict:
    """Privileged desktop handoff; ordinary renderer tokens cannot submit credentials."""
    try:
        return get_connector_service().github_sign_in.connect_token(
            body.token.get_secret_value(),
            account_id=body.account_id,
            revision=body.revision,
            allow_writes=body.allow_writes,
        )
    except ConnectorError as exc:
        raise _http_error(exc) from None


@router.post("/github/login")
def github_login(body: GitHubConnect, request: Request) -> dict:
    """Start a session-owned GitHub browser sign-in."""
    try:
        return get_connector_service().github_sign_in.start(
            request.state.control_session.session_id, **body.model_dump()
        )
    except ConnectorError as exc:
        raise _http_error(exc) from None


@router.post("/github/login/{login_id}/poll")
def github_poll(login_id: str, request: Request) -> dict:
    """Advance one device-flow step, never returning tokens to the UI."""
    try:
        return get_connector_service().github_sign_in.poll(
            login_id, request.state.control_session.session_id
        )
    except ConnectorError as exc:
        raise _http_error(exc) from None


@router.delete("/github/login/{login_id}")
def github_cancel(login_id: str, request: Request) -> dict:
    """Stop polling for the current user's pending sign-in."""
    return get_connector_service().github_sign_in.cancel(
        login_id, request.state.control_session.session_id
    )


@router.get("/providers")
def providers() -> dict:
    """List only registered implementations, with no placeholder Connect actions."""
    return {
        "contract_version": 1,
        "providers": [
            m.model_dump(mode="json")
            for m in get_connector_service().providers.manifests()
        ],
    }


@router.get("/accounts")
def accounts() -> dict:
    """Return account metadata and availability without decrypting credentials."""
    service = get_connector_service()
    available = {manifest.id for manifest in service.providers.manifests()}
    return {
        "accounts": [
            {
                **account.model_dump(mode="json"),
                "provider_available": account.provider_id in available,
            }
            for account in service.store.list()
        ]
    }


@router.patch("/accounts/{account_id}")
def update_account(account_id: AccountId, body: AccountUpdate) -> dict:
    """Change an account label or locally permitted capabilities using revision checks."""
    try:
        account = get_connector_service().update(account_id, **body.model_dump())
        return account.model_dump(mode="json")
    except ConnectorError as exc:
        raise _http_error(exc) from None


@router.post("/accounts/{account_id}/disconnect")
def disconnect_account(account_id: AccountId, body: DisconnectRequest) -> dict:
    """Disconnect locally even if the provider adapter is no longer installed."""
    try:
        account = get_connector_service().disconnect(
            account_id, expected_revision=body.expected_revision
        )
        return account.model_dump(mode="json")
    except ConnectorError as exc:
        raise _http_error(exc) from None


@router.delete("/accounts/{account_id}")
def remove_account(account_id: AccountId, body: DisconnectRequest) -> dict:
    """Forget the saved account and its local credentials using a revision check."""
    try:
        get_connector_service().remove(
            account_id, expected_revision=body.expected_revision
        )
        return {"removed": True}
    except ConnectorError as exc:
        raise _http_error(exc) from None
