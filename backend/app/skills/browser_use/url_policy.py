"""Browser URL policy, domain allowlisting, and SSRF checks."""

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse

from app.agent.run_context import current_interactive


def ip_is_blocked_target(ip: ipaddress._BaseAddress) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def assert_fetch_host_public(host: str) -> None:
    cleaned = (host or "").strip().lower().strip("[]")
    if not cleaned:
        raise ValueError("URL must include a host.")
    try:
        if ip_is_blocked_target(ipaddress.ip_address(cleaned)):
            raise ValueError("URL host resolves to a non-public address.")
        return
    except ValueError as exc:
        if "non-public" in str(exc):
            raise
    try:
        infos = socket.getaddrinfo(cleaned, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise ValueError(f"Could not resolve URL host: {cleaned}") from exc
    for info in infos:
        sockaddr = info[4]
        try:
            if ip_is_blocked_target(ipaddress.ip_address(sockaddr[0])):
                raise ValueError("URL host resolves to a non-public address.")
        except ValueError as exc:
            if "non-public" in str(exc):
                raise


def normalize_fetch_url(raw_url: str) -> str:
    value = str(raw_url or "").strip()
    if not value:
        raise ValueError("URL is required.")
    parsed = urlparse(value)
    if not parsed.scheme:
        value = f"https://{value}"
        parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL must be an http or https URL.")
    return value


def fetch_domain_allowed(url: str, allowed_domains: list[str]) -> bool:
    if not allowed_domains:
        return True
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    for raw_domain in allowed_domains:
        parsed = urlparse(raw_domain if "://" in raw_domain else f"https://{raw_domain}")
        allowed = (parsed.hostname or raw_domain).lower().lstrip(".")
        if host == allowed or host.endswith(f".{allowed}"):
            return True
    return False


def validate_fetch_final_url(
    requested_url: str,
    final_url: str,
    allowed_domains: list[str],
) -> dict[str, Any] | None:
    try:
        normalized_final = normalize_fetch_url(final_url or requested_url)
    except ValueError as exc:
        return {
            "status": "error",
            "reason_code": "invalid_final_url",
            "url": requested_url,
            "final_url": str(final_url or ""),
            "error": str(exc),
        }
    if not fetch_domain_allowed(normalized_final, allowed_domains):
        return {
            "status": "error",
            "reason_code": "redirect_domain_not_allowed",
            "url": requested_url,
            "final_url": normalized_final,
            "allowed_domains": allowed_domains,
            "error": "Redirect target host is not in Settings -> Browser allowed domains.",
        }
    try:
        assert_fetch_host_public(urlparse(normalized_final).hostname or "")
    except ValueError as exc:
        return {
            "status": "error",
            "reason_code": "blocked_redirect_host",
            "url": requested_url,
            "final_url": normalized_final,
            "error": str(exc),
        }
    return None


def allowed_domains(manager: Any) -> list[str]:
    return [
        str(item).strip().lower()
        for item in (getattr(manager, "config", {}) or {}).get("allowed_domains", [])
        if str(item).strip()
    ]


def browser_url_policy(
    manager: Any,
    raw_url: str,
    *,
    action: str,
    interactive: bool | None = None,
) -> tuple[str, dict[str, Any] | None]:
    try:
        normalized_url = normalize_fetch_url(raw_url)
    except ValueError as exc:
        return "", {
            "status": "error",
            "reason_code": "invalid_url",
            "url": str(raw_url or ""),
            "error": str(exc),
        }
    domains = allowed_domains(manager)
    if not fetch_domain_allowed(normalized_url, domains):
        return normalized_url, {
            "status": "error",
            "reason_code": "domain_not_allowed",
            "url": normalized_url,
            "allowed_domains": domains,
            "error": "URL host is not in Settings -> Browser allowed domains.",
        }
    is_interactive = current_interactive() if interactive is None else bool(interactive)
    if not is_interactive and not domains:
        return normalized_url, {
            "status": "blocked",
            "reason_code": "non_interactive_domain_policy_required",
            "url": normalized_url,
            "action": action,
            "error": "Non-interactive browser network actions require an explicit allowed domain policy.",
        }
    if not is_interactive:
        try:
            assert_fetch_host_public(urlparse(normalized_url).hostname or "")
        except ValueError as exc:
            return normalized_url, {
                "status": "error",
                "reason_code": "blocked_host",
                "url": normalized_url,
                "error": str(exc),
            }
    return normalized_url, None
