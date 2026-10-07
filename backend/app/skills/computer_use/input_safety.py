"""Shared checks for all desktop text entry paths."""
from __future__ import annotations

import re
from typing import Any

MAX_TYPED_TEXT_CHARS = 4000
# Substring match: automation ids such as txtPassword, password_input or
# apiKeyInput have no word boundary around the hint.
_CREDENTIAL_LABEL = re.compile(r"password|passcode|otp|2fa|mfa|secret|token|api[ _-]?key", re.IGNORECASE)


def text_entry_error(text: str, metadata: dict[str, Any] | None = None) -> dict | None:
    metadata = metadata or {}
    if len(text) > MAX_TYPED_TEXT_CHARS:
        return {"status": "error", "reason_code": "typed_text_limit_exceeded",
                "error": "Typed text exceeds the maximum allowed character count.", "max_chars": MAX_TYPED_TEXT_CHARS}
    label = " ".join(str(metadata.get(key) or "") for key in
                     ("field", "name", "label", "target_text", "description", "automation_id"))
    if metadata.get("is_password") or _CREDENTIAL_LABEL.search(label):
        return {"status": "blocked", "reason_code": "credential_entry_restricted",
                "error": "Credential-like text entry requires direct local user control."}
    return None
