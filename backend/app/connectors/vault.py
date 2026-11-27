"""Windows user-bound encryption with an injectable OS keyring boundary."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Protocol

from .models import Account, ConnectorError, Credentials


class SecretCipher(Protocol):
    def encrypt(self, data: bytes, binding: bytes) -> bytes:
        """Encrypt and authenticate data for this exact account binding."""
        ...

    def decrypt(self, data: bytes, binding: bytes) -> bytes:
        """Decrypt only when the account and OS protection context match."""
        ...


class WindowsCipher:
    def encrypt(self, data: bytes, binding: bytes) -> bytes:
        """Use user-scoped DPAPI; never fall back to plaintext storage."""
        if os.name != "nt":
            raise ConnectorError("credentials_unavailable")
        import win32crypt

        return win32crypt.CryptProtectData(data, "Monaw connector", binding, None, None, 1)

    def decrypt(self, data: bytes, binding: bytes) -> bytes:
        """Require the same Windows user and account-specific entropy."""
        if os.name != "nt":
            raise ConnectorError("credentials_unavailable")
        import win32crypt

        return win32crypt.CryptUnprotectData(data, binding, None, None, 1)[1]


class CredentialVault:
    def __init__(self, cipher: SecretCipher | None = None):
        """Allow other OS keyrings later without changing account persistence."""
        self.cipher = cipher or WindowsCipher()

    def _binding(self, account: Account) -> bytes:
        """Bind ciphertext to stable identity, independent of labels and grants."""
        material = ["monaw-connectors-v1", account.id, account.provider_id,
                    account.tenant, account.subject, account.auth_method]
        return hashlib.sha256(json.dumps(material, separators=(",", ":")).encode()).digest()

    def seal(self, account: Account, credentials: Credentials) -> bytes:
        """Verify the encrypted round trip before a transaction commits."""
        value = credentials.model_dump(mode="json")
        value["access_token"] = credentials.access_token.get_secret_value()
        value["refresh_token"] = credentials.refresh_token.get_secret_value() if credentials.refresh_token else None
        raw = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
        self._remember(credentials)
        try:
            encrypted = self.cipher.encrypt(raw, self._binding(account))
            if self.cipher.decrypt(encrypted, self._binding(account)) != raw:
                raise ValueError("Encryption verification failed")
            return b"monaw-connectors:v1:" + encrypted
        except Exception:
            raise ConnectorError("credentials_unavailable") from None

    def open(self, account: Account, encrypted: bytes) -> Credentials:
        """Decrypt only internally; API account projections exclude this data."""
        prefix = b"monaw-connectors:v1:"
        try:
            if not encrypted.startswith(prefix):
                raise ValueError("Unsupported vault version")
            raw = self.cipher.decrypt(encrypted[len(prefix):], self._binding(account))
            credentials = Credentials.model_validate_json(raw)
        except Exception:
            raise ConnectorError("credentials_unavailable") from None
        self._remember(credentials)
        return credentials

    def _remember(self, credentials: Credentials) -> None:
        """Reuse process-wide secret redaction without sharing credential access."""
        from app.security.mcp_credentials import remember_secrets

        remember_secrets([credentials.access_token.get_secret_value(),
                          credentials.refresh_token.get_secret_value() if credentials.refresh_token else ""])
