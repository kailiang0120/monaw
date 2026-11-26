"""Indexed history queries independent of connection ownership and orchestration."""

from typing import Any, Protocol


class HistoryReader(Protocol):
    """The database retains responsibility for locking and connection lifetime."""

    def fetchall(self, sql: str, params: tuple = ()) -> list[Any]:
        """Read all rows of an already bounded query under the database lock."""
        ...


class ConversationHistory:
    def __init__(self, reader: HistoryReader) -> None:
        """Bind history queries to the application's synchronized reader."""
        self._reader = reader

    def list(
        self, *, limit: int | None = None, offset: int = 0, search: str = "",
        before_updated_at: str = "", before_id: str = "",
    ) -> list[dict]:
        """Find literal substrings with FTS and paginate by stable activity order."""
        clauses = []
        params: list = []
        query = search.strip()
        if len(query) >= 3 and "\x00" not in query:
            # Quoting keeps quotes, punctuation and FTS operators literal.
            phrase = '"' + query.replace('"', '""') + '"'
            clauses.append("""(c.rowid IN (
                SELECT rowid FROM conversation_search WHERE conversation_search MATCH ?
            ) OR c.id IN (
                SELECT m.conversation_id FROM message_search
                JOIN messages m ON m.id = message_search.rowid WHERE message_search MATCH ?
            ))""")
            params.extend([phrase, phrase])
        elif query:
            # A trigram index cannot match one or two characters. Preserve these
            # searches (including short CJK terms) with the bounded legacy query.
            clauses.append("""(instr(lower(c.title), lower(?)) > 0
                OR instr(lower(c.workspace_dir), lower(?)) > 0
                OR EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id
                           AND instr(lower(m.content), lower(?)) > 0))""")
            params.extend([query] * 3)
        if before_updated_at and before_id:
            clauses.append("(c.updated_at < ? OR (c.updated_at = ? AND c.id < ?))")
            params.extend([before_updated_at, before_updated_at, before_id])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        paging = " LIMIT ? OFFSET ?" if limit is not None else ""
        if limit is not None:
            params.extend([max(1, limit), max(0, offset)])
        rows = self._reader.fetchall(
            "SELECT c.id, c.title, c.created_at, c.updated_at, c.workspace_dir FROM conversations c"
            + where + " ORDER BY c.updated_at DESC, c.id DESC" + paging,
            tuple(params),
        )
        return [dict(row) for row in rows]
