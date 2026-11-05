"""Observable destination facts, not a network sandbox for arbitrary code."""
from __future__ import annotations

from urllib.parse import urlsplit


def canonical_origin(url: str) -> str:
    try:
        parsed = urlsplit(str(url or ""))
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("destination_origin_required")
        scheme = parsed.scheme.lower()
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
        if ":" in host:
            host = f"[{host}]"
        suffix = f":{port}" if port and port != {"http": 80, "https": 443}[scheme] else ""
        return f"{scheme}://{host}{suffix}"
    except (ValueError, UnicodeError) as exc:
        raise ValueError("destination_origin_required") from exc


def ensure_same_origin(actual_url: str, expected_origin: str) -> str:
    actual = canonical_origin(actual_url)
    if actual != canonical_origin(expected_origin):
        raise ValueError("destination_origin_changed")
    return actual


def observable_argument_destinations(arguments: dict) -> list[str]:
    """Collect explicit URL arguments only; absence does not prove no egress."""
    found: set[str] = set()
    def visit(value):
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str) and value.lower().startswith(("http://", "https://")):
            found.add(canonical_origin(value))
    visit(arguments)
    return sorted(found)
