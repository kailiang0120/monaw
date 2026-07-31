"""Encrypted local artifacts for support diagnostics."""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet


KEY_FILE_NAME = ".support-bundle.key"


def _key_path(root: Path) -> Path:
    return root / KEY_FILE_NAME


def _load_or_create_key(root: Path) -> bytes:
    root.mkdir(parents=True, exist_ok=True)
    path = _key_path(root)
    if path.exists():
        return path.read_bytes().strip()
    key = Fernet.generate_key()
    path.write_bytes(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def encrypt_bytes(root: Path, payload: bytes) -> bytes:
    return Fernet(_load_or_create_key(root)).encrypt(payload)


def decrypt_bytes(root: Path, payload: bytes) -> bytes:
    return Fernet(_load_or_create_key(root)).decrypt(payload)
