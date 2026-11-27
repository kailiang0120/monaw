-- Public metadata and opaque OS-encrypted credentials commit atomically.
CREATE TABLE connector_accounts (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    tenant TEXT NOT NULL,
    subject TEXT NOT NULL,
    revision INTEGER NOT NULL,
    metadata_json TEXT NOT NULL,
    credential_blob BLOB,
    UNIQUE(provider_id, tenant, subject)
);
