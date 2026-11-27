"""Restricted Composio Sessions API; no SDK tools are exposed directly to the model."""

from contextlib import contextmanager
import json
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx

from .models import ConnectorError
from .composio_catalog import FIXED_ARGUMENTS, MANIFEST_BY_ID, SCOPES, TOOLS

ORIGIN = "https://backend.composio.dev"
MAX_RESPONSE = 256 * 1024


def identifier(value):
    """Bound remote identifiers before they enter paths or persistent bindings."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value):
        raise ConnectorError("invalid_result")
    return value


def login_url(value):
    """Only open Composio's documented hosted Connect Link origin."""
    try:
        url = urlsplit(value)
        if (
            len(value) > 16000
            or url.scheme != "https"
            or url.netloc != "connect.composio.dev"
            or not url.path.startswith("/link/")
            or url.fragment
            or "\\" in value
        ):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise ConnectorError("authorization_invalid") from None


class BoundedStream(httpx.SyncByteStream):
    def __init__(self, stream):
        """Wrap the network stream before the SDK can buffer or parse it."""
        self.stream = stream

    def __iter__(self):
        """Reject oversized responses while reading, including decompression-safe JSON later."""
        size = 0
        for chunk in self.stream:
            size += len(chunk)
            if size > MAX_RESPONSE:
                raise ConnectorError("invalid_result")
            yield chunk

    def close(self):
        """Release the underlying connection on success or any parsing failure."""
        self.stream.close()


class BoundedTransport(httpx.BaseTransport):
    def __init__(self):
        """Do not inherit shell proxies or enable transport retries."""
        self.transport = httpx.HTTPTransport(trust_env=False, retries=0)

    def handle_request(self, request):
        """Keep the user's API key on the fixed Composio API origin."""
        if (
            str(request.url.copy_with(path="/", query=None, fragment=None))
            != ORIGIN + "/"
        ):
            raise ConnectorError("authorization_invalid")
        request.headers["accept-encoding"] = "identity"
        response = self.transport.handle_request(request)
        if response.headers.get("content-encoding", "identity") != "identity":
            response.close()
            raise ConnectorError("invalid_result")
        response.stream = BoundedStream(response.stream)
        return response

    def close(self):
        """Close sockets when the short-lived SDK client finishes."""
        self.transport.close()


class ComposioGateway:
    def __init__(self):
        """Cache only reviewed tool input schemas, never credentials or provider results."""
        self.schemas = {}
        self.schema_lock = threading.Lock()

    @contextmanager
    def client(self, key, remaining=lambda: 20.0):
        """Create a bounded, non-retrying SDK client and sanitize every SDK exception."""
        from composio_client import Composio, ProjectAuth

        try:
            with httpx.Client(
                transport=BoundedTransport(), trust_env=False, follow_redirects=False
            ) as http:
                with Composio(
                    auth=ProjectAuth(key),
                    base_url=ORIGIN,
                    http_client=http,
                    max_retries=0,
                    timeout=min(20.0, remaining()),
                ) as client:
                    yield client
            remaining()
        except ConnectorError:
            raise
        except Exception as error:
            status = getattr(error, "status_code", None)
            code = {401: "needs_reauth", 403: "scope_missing", 429: "rate_limited"}.get(
                status, "provider_error"
            )
            if isinstance(error, httpx.TimeoutException):
                code = "timeout"
            raise ConnectorError(code) from None

    def validate(self, key):
        """Verify a Platform project key without executing a tool or exposing its accounts."""
        with self.client(key) as client:
            client.connected_accounts.list(limit=1)

    def auth_config(self, key, provider):
        """Provision managed OAuth with explicit read-only scopes, not toolkit defaults."""
        if provider not in SCOPES:
            raise ConnectorError("provider_unavailable")
        scopes = SCOPES[provider]
        with self.client(key) as client:
            result = client.auth_configs.create(
                toolkit={"slug": provider},
                auth_config={
                    "type": "use_composio_managed_auth",
                    "name": "Monaw " + provider + " read only",
                    "credentials": {"scopes": scopes} if scopes else {},
                    "is_enabled_for_tool_router": True,
                },
            )
            if (
                result.toolkit.slug != provider
                or not result.auth_config.is_composio_managed
            ):
                raise ConnectorError("invalid_result")
            return identifier(result.auth_config.id)

    def session(
        self,
        key,
        user,
        provider,
        connection=None,
        auth_config=None,
        remaining=lambda: 20.0,
    ):
        """Pin the account and exact reviewed actions; disable metatool side paths."""
        if provider not in TOOLS:
            raise ConnectorError("provider_unavailable")
        with self.client(key, remaining) as client:
            config = dict(
                user_id=user,
                toolkits={"enable": [provider]},
                tools={provider: {"enable": list(TOOLS[provider].values())}},
                instant=False,
                workbench={"enable": False, "enable_proxy_execution": False},
                proxy_execute={"enable": False},
                manage_connections={"enable": False},
                execute={"enable_multi_execute": False},
            )
            if connection:
                config["connected_accounts"] = {provider: [identifier(connection)]}
            if auth_config:
                config["auth_configs"] = {provider: identifier(auth_config)}
            result = client.tool_router.session.create(**config)
            return identifier(result.session_id)

    def link(self, key, session, provider):
        """Let Composio provision managed authentication and return only linking metadata."""
        with self.client(key) as client:
            result = client.tool_router.session.link(session, toolkit=provider)
            return identifier(result.connected_account_id), login_url(
                result.redirect_url
            )

    def account(self, key, user, provider, connection, remaining=lambda: 20.0):
        """Use server-side ownership filters; never trust a callback's account identity."""
        with self.client(key, remaining) as client:
            result = client.connected_accounts.list(
                user_ids=[user],
                toolkit_slugs=[provider],
                connected_account_ids=[identifier(connection)],
                account_type="PRIVATE",
                limit=1,
            )
            rows = result.items
            if not rows:
                raise ConnectorError("account_not_found")
            row = rows[0]
            if (
                row.id != connection
                or row.toolkit.slug != provider
                or row.is_disabled
                or row.auth_config.is_disabled
            ):
                raise ConnectorError("account_changed")
            return {"status": row.status, "auth_config": identifier(row.auth_config.id)}

    def execute(
        self, key, session, provider, capability, arguments, remaining=lambda: 20.0
    ):
        """Execute one reviewed action through a pinned session, never a generic proxy."""
        slug = TOOLS.get(provider, {}).get(capability)
        if not slug:
            raise ConnectorError("capability_disabled")
        with self.client(key, remaining) as client:
            with self.schema_lock:
                cached = self.schemas.get(slug)
            if not cached or cached[0] < time.monotonic():
                tool = client.tools.retrieve(slug)
                if tool.slug != slug or tool.toolkit.slug != provider:
                    raise ConnectorError("invalid_result")
                cached = (time.monotonic() + 300, tool.input_parameters)
                with self.schema_lock:
                    self.schemas[slug] = cached
            inputs = tool_arguments(provider, capability, arguments, cached[1])
            result = client.tool_router.session.execute(
                session,
                tool_slug=slug,
                arguments=inputs,
                enable_auto_workbench_offload=False,
                timeout=min(20.0, remaining()),
            )
            if result.result_type != "completed":
                raise ConnectorError("provider_error")
            data = result.data
            if (
                not isinstance(data, dict)
                or len(json.dumps(data).encode()) > MAX_RESPONSE
            ):
                raise ConnectorError("invalid_result")
            return data

    def delete_session(self, key, session):
        """Best-effort cleanup of a session, without revoking the user's provider grant."""
        try:
            with self.client(key, lambda: 5.0) as client:
                client.tool_router.session.delete(session)
        except ConnectorError:
            pass


def tool_arguments(provider, capability, arguments, schema):
    """Map reviewed aliases against the live schema without exposing arbitrary remote fields."""
    if provider not in {"gmail", "outlook"}:
        return native_arguments(provider, capability, arguments, schema)
    properties = schema.get("properties", {})
    aliases = {
        "messageId": ["message_id", "messageId"],
        "maxResults": ["max_results"],
        "pageToken": ["page_token"],
        "mailFolderId": ["folder", "folder_id", "mail_folder_id"],
        "filter": ["filter", "$filter"],
        "top": ["top", "$top"],
        "query": ["query"],
    }
    values = dict(arguments)
    if capability == "fetch_emails":
        values.setdefault("maxResults", 20)
    if capability in {"list_messages", "list_mail_folders"}:
        values.setdefault("top", 20)
    result = {}
    for key, value in values.items():
        choices = aliases.get(key, [])
        if key == "nextLink":
            choices = ["skip_token"]
            if value.startswith("https:"):
                url = urlsplit(value)
                path = r"/v1\.0/me/(?:mailFolders(?:/[A-Za-z0-9_=-]+)?/)?messages"
                if capability == "list_mail_folders":
                    path = r"/v1\.0/me/mailFolders"
                if (
                    url.netloc != "graph.microsoft.com"
                    or not re.fullmatch(path, url.path)
                    or url.fragment
                ):
                    raise ConnectorError("invalid_arguments")
                params = parse_qs(url.query)
                if "$skiptoken" in params:
                    value = params["$skiptoken"][0]
                elif params.get("$skip", [""])[0].isdigit():
                    value = int(params["$skip"][0])
                    choices = ["skip", "$skip"]
                else:
                    raise ConnectorError("invalid_arguments")
        selected = next((name for name in choices if name in properties), None)
        if selected is None:
            raise ConnectorError("invalid_arguments")
        result[selected] = value
    for name in ("user_id", "userId"):
        if name in properties:
            result[name] = "me"
    if any(name not in result for name in schema.get("required", [])):
        raise ConnectorError("invalid_result")
    return result


def native_arguments(provider, capability, arguments, schema):
    """Validate reviewed native fields, bounded page defaults, and fixed account selectors."""
    from jsonschema import Draft202012Validator

    manifest = MANIFEST_BY_ID.get(provider)
    action = (
        next((item for item in manifest.capabilities if item.id == capability), None)
        if manifest
        else None
    )
    if action is None:
        raise ConnectorError("capability_disabled")
    if not Draft202012Validator(action.parameters).is_valid(arguments):
        raise ConnectorError("invalid_arguments")
    result = dict(arguments)
    for name, definition in action.parameters["properties"].items():
        if "default" in definition:
            result.setdefault(name, definition["default"])
    result.update(FIXED_ARGUMENTS[provider, capability])
    properties = schema.get("properties", {})
    if any(name not in properties for name in result):
        raise ConnectorError("invalid_result")
    if not Draft202012Validator(schema).is_valid(result):
        raise ConnectorError("invalid_result")
    return result
