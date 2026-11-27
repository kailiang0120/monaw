-- One user-owned Composio project key, encrypted with the existing OS cipher.
CREATE TABLE composio_configuration (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    user_id TEXT NOT NULL,
    generation TEXT NOT NULL,
    key_blob BLOB
);
CREATE TABLE composio_auth_configs (
    generation TEXT NOT NULL,
    provider TEXT NOT NULL,
    config_id TEXT NOT NULL,
    PRIMARY KEY (generation, provider)
);

-- Old runtime grants cannot authorize a different connector implementation.
UPDATE connector_accounts
SET revision = revision + 1,
    metadata_json = json_set(metadata_json, '$.revision', revision + 1,
        '$.state', 'disconnected', '$.enabled', json('false'),
        '$.enabled_capabilities', json('[]')),
    credential_blob = NULL
WHERE EXISTS (
    SELECT 1 FROM json_each(metadata_json, '$.granted_scopes')
    WHERE value = 'openconnector:bound'
);
