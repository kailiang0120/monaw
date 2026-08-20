"""Forward-only SQLite migration runner."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MIGRATION_RE = re.compile(r"^(\d{4})_(.+)\.sql$")


def _migration_dir() -> Path:
    return Path(__file__).resolve().parent


def _iter_migrations(migrations_dir: Path) -> list[tuple[int, str, Path]]:
    migrations: list[tuple[int, str, Path]] = []
    for path in migrations_dir.glob("*.sql"):
        match = MIGRATION_RE.match(path.name)
        if match is None:
            continue
        migrations.append((int(match.group(1)), match.group(2), path))
    return sorted(migrations, key=lambda item: item[0])


def _iter_sql_statements(script: str):
    """Yield complete SQLite statements without using executescript.

    ``Connection.executescript`` commits implicitly, which makes a table
    rebuild impossible to roll back as one migration. SQLite's statement
    completeness checker handles quoted semicolons while preserving the
    migration text for ``Connection.execute`` inside our savepoint.
    """
    buffer = ""
    for line in str(script or "").splitlines(keepends=True):
        buffer += line
        if not sqlite3.complete_statement(buffer):
            continue
        statement = buffer.strip()
        buffer = ""
        if statement:
            yield statement
    if buffer.strip():
        yield buffer.strip()


def apply_migrations(
    conn: sqlite3.Connection,
    *,
    migrations_dir: Path | None = None,
) -> list[int]:
    """Apply unrecorded SQL migrations and return applied versions."""
    root = migrations_dir or _migration_dir()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = {
        int(row[0])
        for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    applied_now: list[int] = []
    for version, name, path in _iter_migrations(root):
        if version in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        savepoint = f"migration_{version:04d}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            for statement in _iter_sql_statements(sql):
                conn.execute(statement)
            conn.execute(
                """
                INSERT INTO schema_migrations (version, name, applied_at)
                VALUES (?, ?, ?)
                """,
                (version, name, datetime.now(timezone.utc).isoformat()),
            )
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        applied_now.append(version)
    return applied_now
