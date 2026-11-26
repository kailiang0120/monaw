CREATE VIRTUAL TABLE conversation_search USING fts5(
    title, workspace_dir, content='conversations', content_rowid='rowid', tokenize='trigram'
);
CREATE VIRTUAL TABLE message_search USING fts5(
    content, content='messages', content_rowid='id', tokenize='trigram'
);

CREATE TRIGGER conversations_search_insert AFTER INSERT ON conversations BEGIN
    INSERT INTO conversation_search(rowid, title, workspace_dir) VALUES (new.rowid, new.title, new.workspace_dir);
END;
CREATE TRIGGER conversations_search_delete AFTER DELETE ON conversations BEGIN
    INSERT INTO conversation_search(conversation_search, rowid, title, workspace_dir)
        VALUES ('delete', old.rowid, old.title, old.workspace_dir);
END;
CREATE TRIGGER conversations_search_update AFTER UPDATE OF title, workspace_dir ON conversations BEGIN
    INSERT INTO conversation_search(conversation_search, rowid, title, workspace_dir)
        VALUES ('delete', old.rowid, old.title, old.workspace_dir);
    INSERT INTO conversation_search(rowid, title, workspace_dir) VALUES (new.rowid, new.title, new.workspace_dir);
END;

CREATE TRIGGER messages_search_insert AFTER INSERT ON messages BEGIN
    INSERT INTO message_search(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER messages_search_delete AFTER DELETE ON messages BEGIN
    INSERT INTO message_search(message_search, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER messages_search_update AFTER UPDATE OF content ON messages BEGIN
    INSERT INTO message_search(message_search, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO message_search(rowid, content) VALUES (new.id, new.content);
END;

INSERT INTO conversation_search(conversation_search) VALUES ('rebuild');
INSERT INTO message_search(message_search) VALUES ('rebuild');
