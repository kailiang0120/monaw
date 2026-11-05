from types import SimpleNamespace

import pytest

from app.agent.action_security.destination_policy import canonical_origin, ensure_same_origin
from app.skills.browser_use.url_policy import ensure_allowed_current_origin


def test_observed_origin_must_match_allowlist_and_approved_origin():
    manager = SimpleNamespace(config={"allowed_domains": ["example.com"]})
    assert canonical_origin("HTTPS://EXAMPLE.COM:443/path") == "https://example.com"
    assert ensure_allowed_current_origin(manager, "https://account.example.com/home", interactive=True) == "https://account.example.com"
    with pytest.raises(ValueError, match="domain_not_allowed"):
        ensure_allowed_current_origin(manager, "https://example.com.evil.test/", interactive=True)
    with pytest.raises(ValueError, match="destination_origin_changed"):
        ensure_same_origin("https://example.com:8443", "https://example.com")
    for url in ("https://user:secret@example.com/", "file:///secret", "https://example.com:bad/"):
        with pytest.raises(ValueError):
            canonical_origin(url)
