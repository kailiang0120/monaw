"""Transactional connector persistence using Monaw's existing SQLite database."""

from __future__ import annotations

import sqlite3

from app.agent.database import Database

from .models import Account, ConnectorError


class AccountStore:
    def __init__(self, db: Database):
        """Use the shared DB lock and migration lifecycle."""
        self.db = db

    def list(self) -> list[Account]:
        """Read public metadata without selecting credential ciphertext."""
        return [Account.model_validate_json(row["metadata_json"]) for row in
                self.db.fetchall("SELECT metadata_json FROM connector_accounts ORDER BY provider_id, id")]

    def get(self, account_id: str) -> Account:
        """Resolve stable account IDs rather than a provider's default account."""
        row = self.db.fetchone("SELECT metadata_json FROM connector_accounts WHERE id = ?", (account_id,))
        if row is None:
            raise ConnectorError("account_not_found")
        return Account.model_validate_json(row["metadata_json"])

    def credentials(self, account: Account) -> bytes:
        """Refuse credential reads through a stale account snapshot."""
        row = self.db.fetchone("SELECT credential_blob FROM connector_accounts WHERE id = ? AND revision = ?",
                               (account.id, account.revision))
        if row is None:
            raise ConnectorError("account_changed")
        if row["credential_blob"] is None:
            raise ConnectorError("needs_reauth")
        return bytes(row["credential_blob"])

    def insert(self, account: Account, encrypted: bytes) -> None:
        """Create identity and credential atomically, including duplicate protection."""
        try:
            with self.db.transaction() as conn:
                conn.execute("INSERT INTO connector_accounts VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (account.id, account.provider_id, account.tenant, account.subject,
                              account.revision, account.model_dump_json(), encrypted))
        except sqlite3.IntegrityError:
            raise ConnectorError("account_exists") from None
        self._publish(account)

    def replace(self, account: Account, expected_revision: int, *, encrypted: bytes | None = None,
                clear_credentials: bool = False) -> None:
        """Compare and swap prevents stale refreshes or settings edits restoring access."""
        with self.db.transaction() as conn:
            fields = "revision = ?, metadata_json = ?"
            values: list = [account.revision, account.model_dump_json()]
            if encrypted is not None or clear_credentials:
                fields += ", credential_blob = ?"
                values.append(encrypted)
            cursor = conn.execute(f"UPDATE connector_accounts SET {fields} WHERE id = ? AND revision = ?",
                                  (*values, account.id, expected_revision))
            if cursor.rowcount != 1:
                raise ConnectorError("account_changed")
        self._publish(account)

    def _publish(self, account: Account) -> None:
        """Notify subscribers after commit without including identity or credentials."""
        from app.agent.ui_events import publish_ui_event

        publish_ui_event("connectors.changed", {"account_id": account.id, "revision": account.revision})

    def delete(self, account: Account) -> None:
        """Forget the local identity and encrypted credentials in one transaction."""
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM connector_accounts WHERE id = ? AND revision = ?",
                                  (account.id, account.revision))
            if cursor.rowcount != 1:
                raise ConnectorError("account_changed")
        self._publish(account)
