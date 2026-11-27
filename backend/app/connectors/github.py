"""GitHub.com connector using fixed REST endpoints and account-scoped credentials."""

from __future__ import annotations

import base64
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from .models import (
    Capability,
    ConnectorError,
    ConnectorResult,
    Credentials,
    Identity,
    ProviderManifest,
    RefreshResult,
)

API_VERSION = "2026-03-10"
AUTHENTICATED = "github:authenticated"
REPO = {
    "type": "string",
    "pattern": r"^(?!\.{1,2}/)[A-Za-z0-9_.-]+/(?!\.{1,2}$)[A-Za-z0-9_.-]+$",
    "maxLength": 250,
}
PAGE = {"type": "integer", "minimum": 1, "maximum": 1000, "default": 1}
NUMBER = {"type": "integer", "minimum": 1}


def capability(name, description, properties, required=(), action="read"):
    """Build bounded schemas shared by model discovery and runtime validation."""
    return Capability(
        id=name,
        description=description,
        action=action,
        required_scopes={AUTHENTICATED},
        parameters={
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
    )


MANIFEST = ProviderManifest(
    id="github",
    name="GitHub",
    category="development",
    description="Repositories, issues, pull requests, and workflows.",
    version="1",
    auth_methods={"api_key", "device_code"},
    capabilities=(
        capability(
            "list_repositories",
            "List repositories accessible to this account. Pages contain up to 30 repositories.",
            {"page": PAGE},
        ),
        capability(
            "search_issues",
            "Search GitHub issues and pull requests. Use repo:owner/name to narrow the query.",
            {"query": {"type": "string", "minLength": 1, "maxLength": 1000}, "page": PAGE},
            ("query",),
        ),
        capability(
            "get_issue",
            "Read a GitHub issue or pull request discussion, including a page of comments.",
            {"repository": REPO, "number": NUMBER, "page": PAGE},
            ("repository", "number"),
        ),
        capability(
            "list_pull_requests",
            "List pull requests for a repository.",
            {
                "repository": REPO,
                "state": {"enum": ["open", "closed", "all"], "default": "open"},
                "page": PAGE,
            },
            ("repository",),
        ),
        capability(
            "get_pull_request",
            "Read pull request details and a page of changed files with patches.",
            {"repository": REPO, "number": NUMBER, "page": PAGE},
            ("repository", "number"),
        ),
        capability(
            "get_file",
            "Read a UTF-8 file or list a directory at a GitHub ref; files are limited to 128 KiB.",
            {
                "repository": REPO,
                "path": {"type": "string", "maxLength": 2000},
                "ref": {"type": "string", "minLength": 1, "maxLength": 250},
            },
            ("repository", "path"),
        ),
        capability(
            "list_workflow_runs",
            "Read GitHub Actions workflow run status for a repository.",
            {"repository": REPO, "page": PAGE},
            ("repository",),
        ),
        capability(
            "get_workflow_jobs",
            "Read jobs and step outcomes for a GitHub Actions run.",
            {"repository": REPO, "run_id": NUMBER, "page": PAGE},
            ("repository", "run_id"),
        ),
        capability(
            "create_issue",
            "Create one GitHub issue after reviewing its repository, title and body.",
            {
                "repository": REPO,
                "title": {"type": "string", "minLength": 1, "maxLength": 256},
                "body": {"type": "string", "maxLength": 30000},
            },
            ("repository", "title", "body"),
            "create",
        ),
        capability(
            "create_comment",
            "Post one comment on a GitHub issue or pull request.",
            {
                "repository": REPO,
                "number": NUMBER,
                "body": {"type": "string", "minLength": 1, "maxLength": 30000},
            },
            ("repository", "number", "body"),
            "send_message",
        ),
        capability(
            "create_pull_request",
            "Open one pull request between existing GitHub branches.",
            {
                "repository": REPO,
                "title": {"type": "string", "minLength": 1, "maxLength": 256},
                "body": {"type": "string", "maxLength": 30000},
                "head": {"type": "string", "minLength": 1, "maxLength": 250},
                "base": {"type": "string", "minLength": 1, "maxLength": 250},
                "draft": {"type": "boolean", "default": True},
            },
            ("repository", "title", "body", "head", "base"),
            "create",
        ),
    ),
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Never forward authentication to a redirect destination."""
        return None


def request_json(
    path: str,
    *,
    token: str = "",
    method: str = "GET",
    data=None,
    query=None,
    timeout: float = 20,
    oauth: bool = False,
) -> tuple[object, dict]:
    """Send a bounded request to a fixed GitHub origin; return no raw errors."""
    if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
        raise ConnectorError("invalid_arguments")
    if oauth and path not in {"/login/device/code", "/login/oauth/access_token"}:
        raise ConnectorError("invalid_arguments")
    origin = "https://github.com" if oauth else "https://api.github.com"
    url = origin + path + ("?" + urllib.parse.urlencode(query) if query else "")
    headers = {
        "Accept": "application/json" if oauth else "application/vnd.github+json",
        "User-Agent": "Monaw",
    }
    if not oauth:
        headers["X-GitHub-Api-Version"] = API_VERSION
    if token:
        headers["Authorization"] = "Bearer " + token
    body = None if data is None else json.dumps(data).encode()
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.build_opener(NoRedirect()).open(
            request, timeout=min(20, max(0.1, timeout))
        ) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ConnectorError("invalid_result", uncertain=method != "GET")
            return json.loads(raw), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        headers = {key.lower(): value for key, value in exc.headers.items()}
        status = exc.code
        exc.close()
        if status == 401:
            raise ConnectorError("needs_reauth") from None
        if status == 429 or (
            status == 403 and ("retry-after" in headers or headers.get("x-ratelimit-remaining") == "0")
        ):
            try:
                delay = float(
                    headers.get("retry-after")
                    or max(1, float(headers.get("x-ratelimit-reset", 0)) - time.time())
                )
            except ValueError:
                delay = 60
            raise ConnectorError("rate_limited", retry_after=delay) from None
        if status in {403, 404}:
            raise ConnectorError("scope_missing") from None
        raise ConnectorError("provider_error", uncertain=method != "GET" and status >= 500) from None
    except (TimeoutError, socket.timeout):
        raise ConnectorError("timeout", uncertain=method != "GET") from None
    except (urllib.error.URLError, ValueError):
        raise ConnectorError("provider_error", uncertain=method != "GET") from None


def github_configuration() -> dict:
    """Read public registration details; never accept or distribute an app secret."""
    from pathlib import Path

    path = Path(__file__).with_name("github-app.json")
    bundled = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    client_id = os.environ.get("MONAW_GITHUB_CLIENT_ID", bundled.get("client_id", "")).strip()
    slug = os.environ.get("MONAW_GITHUB_APP_SLUG", bundled.get("slug", "")).strip()
    import re

    if client_id and not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", client_id):
        raise ConnectorError("provider_unavailable")
    if slug and not re.fullmatch(r"[a-z0-9-]{1,100}", slug):
        raise ConnectorError("provider_unavailable")
    return {
        "client_id": client_id,
        "installation_url": f"https://github.com/apps/{slug}/installations/new" if slug else "",
    }


def verified_identity(token: str) -> Identity:
    """Use GitHub's immutable user ID rather than trusting a supplied username."""
    from app.security.mcp_credentials import remember_secrets

    remember_secrets([token])
    user, _ = request_json("/user", token=token)
    if not isinstance(user, dict) or not isinstance(user.get("id"), int) or not user.get("login"):
        raise ConnectorError("authorization_invalid")
    # This grant attests authentication only. GitHub enforces per-repository permissions on every request.
    return Identity(
        subject=str(user["id"]), tenant="github.com", label=str(user["login"]), granted_scopes={AUTHENTICATED}
    )


def token_credentials(payload: dict, client_id: str) -> Credentials:
    """Validate OAuth tokens and convert relative expiry to an absolute timestamp."""
    if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str):
        raise ConnectorError("needs_reauth")
    try:
        return Credentials(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            client_id=client_id,
            expires_at=time.time() + int(payload["expires_in"]) if payload.get("expires_in") else None,
        )
    except (ValueError, TypeError, OverflowError):
        raise ConnectorError("needs_reauth") from None


def brief(value: dict) -> dict:
    """Keep useful API fields without returning embedded credentials or huge nested objects."""
    fields = (
        "id",
        "number",
        "name",
        "full_name",
        "path",
        "type",
        "sha",
        "size",
        "title",
        "body",
        "state",
        "html_url",
        "private",
        "description",
        "updated_at",
        "created_at",
        "merged",
        "mergeable",
        "draft",
        "status",
        "conclusion",
        "head_branch",
        "head_sha",
        "run_number",
        "filename",
        "additions",
        "deletions",
        "changes",
        "patch",
        "steps",
    )
    result = {key: value[key] for key in fields if key in value}
    for key in ("user", "assignee"):
        if isinstance(value.get(key), dict):
            result[key] = {"login": value[key].get("login"), "html_url": value[key].get("html_url")}
    for key in ("head", "base"):
        if isinstance(value.get(key), dict):
            result[key] = {"ref": value[key].get("ref"), "sha": value[key].get("sha")}
    return result


class GitHubProvider:
    manifest = MANIFEST

    def refresh(self, credentials, context) -> RefreshResult:
        """Refresh device-flow tokens without embedding a GitHub client secret."""
        client_id = credentials.client_id
        if not client_id or not credentials.refresh_token:
            raise ConnectorError("needs_reauth")
        result, _ = request_json(
            "/login/oauth/access_token",
            oauth=True,
            method="POST",
            data={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": credentials.refresh_token.get_secret_value(),
            },
            timeout=context.remaining(),
        )
        return RefreshResult(credentials=token_credentials(result, client_id))

    def action_target(self, capability, arguments, account) -> dict:
        """Describe the exact repository and issue/PR destination for approval."""
        repository = arguments.get("repository", "")
        url = f"https://github.com/{repository}" if repository else "https://github.com"
        if arguments.get("number"):
            url += f"/issues/{arguments['number']}"
        return {
            "destination": url,
            "preview": {
                "account": account.label,
                "repository": repository,
                **{
                    key: arguments[key]
                    for key in ("number", "title", "body", "head", "base", "draft")
                    if key in arguments
                },
            },
        }

    def invoke(self, capability, arguments, credentials, context) -> ConnectorResult:
        """Execute one bounded GitHub action; writes are never retried here."""
        token = credentials.access_token.get_secret_value()
        repository = arguments.get("repository", "")
        root = "/repos/" + "/".join(urllib.parse.quote(part, safe="") for part in repository.split("/"))
        page = arguments.get("page", 1)
        query = {"page": page, "per_page": 30}
        method, body, collection = "GET", None, None
        name = capability.id
        if name == "list_repositories":
            path = "/user/repos"
            query["sort"] = "updated"
        elif name == "search_issues":
            path, collection = "/search/issues", "items"
            query["q"] = arguments["query"]
        elif name == "get_issue":
            path = f"{root}/issues/{arguments['number']}"
            issue, _ = request_json(path, token=token, timeout=context.remaining())
            comments, headers = request_json(
                path + "/comments", token=token, query=query, timeout=context.remaining()
            )
            return self._page(
                {"issue": brief(issue), "comments": [brief(c) for c in comments]},
                headers,
                page,
                issue.get("html_url"),
            )
        elif name == "list_pull_requests":
            path = root + "/pulls"
            query["state"] = arguments.get("state", "open")
        elif name == "get_pull_request":
            path = f"{root}/pulls/{arguments['number']}"
            pull, _ = request_json(path, token=token, timeout=context.remaining())
            files, headers = request_json(
                path + "/files", token=token, query=query, timeout=context.remaining()
            )
            return self._page(
                {"pull_request": brief(pull), "files": [brief(f) for f in files]},
                headers,
                page,
                pull.get("html_url"),
            )
        elif name == "get_file":
            parts = arguments["path"].replace("\\", "/").split("/")
            if any(part in {".", ".."} for part in parts):
                raise ConnectorError("invalid_arguments")
            path = root + "/contents/" + "/".join(urllib.parse.quote(part, safe="") for part in parts)
            data, _ = request_json(
                path,
                token=token,
                query={"ref": arguments["ref"]} if "ref" in arguments else None,
                timeout=context.remaining(),
            )
            if isinstance(data, list):
                return ConnectorResult(data=[brief(item) for item in data])
            if (
                data.get("type") != "file"
                or data.get("encoding") != "base64"
                or data.get("size", 0) > 128 * 1024
            ):
                raise ConnectorError("invalid_result")
            try:
                content = base64.b64decode(data["content"]).decode("utf-8")
            except (ValueError, UnicodeError):
                raise ConnectorError("invalid_result") from None
            return ConnectorResult(data={**brief(data), "content": content}, links=(data["html_url"],))
        elif name == "list_workflow_runs":
            path, collection = root + "/actions/runs", "workflow_runs"
        elif name == "get_workflow_jobs":
            path, collection = f"{root}/actions/runs/{arguments['run_id']}/jobs", "jobs"
        elif name == "create_issue":
            path, method, body = root + "/issues", "POST", {key: arguments[key] for key in ("title", "body")}
        elif name == "create_comment":
            path, method, body = (
                f"{root}/issues/{arguments['number']}/comments",
                "POST",
                {"body": arguments["body"]},
            )
        elif name == "create_pull_request":
            path, method = root + "/pulls", "POST"
            body = {key: arguments[key] for key in ("title", "body", "head", "base")}
            body["draft"] = arguments.get("draft", True)
        else:
            raise ConnectorError("capability_disabled")
        data, headers = request_json(
            path,
            token=token,
            method=method,
            data=body,
            query=query if method == "GET" else None,
            timeout=context.remaining(),
        )
        if collection:
            data = data[collection]
        projected = [brief(item) for item in data] if isinstance(data, list) else brief(data)
        return self._page(projected, headers, page, data.get("html_url") if isinstance(data, dict) else None)

    def _page(self, data, headers, page, link=None) -> ConnectorResult:
        """Return page numbers instead of following provider-supplied pagination URLs."""
        next_page = (
            str(page + 1)
            if any(key.lower() == "link" and 'rel="next"' in value for key, value in headers.items())
            else None
        )
        return ConnectorResult(data=data, next_cursor=next_page, links=(link,) if link else ())
