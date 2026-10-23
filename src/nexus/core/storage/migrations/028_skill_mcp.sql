ALTER TABLE skill_runs ADD COLUMN permission_context_id TEXT REFERENCES conversations(conversation_id);
ALTER TABLE skill_runs ADD COLUMN permission_fingerprint TEXT;
CREATE TABLE mcp_registrations (
    registration_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    node_id TEXT NOT NULL REFERENCES identities(identity_id),
    server TEXT NOT NULL,
    revision INTEGER NOT NULL,
    configuration_json TEXT NOT NULL,
    configuration_digest TEXT NOT NULL,
    catalog_json TEXT,
    catalog_digest TEXT,
    state TEXT NOT NULL CHECK(state IN ('pending','active','inactive','failed','removed')),
    created_at TEXT NOT NULL,
    UNIQUE(owner_id,node_id,server,revision)
);
CREATE UNIQUE INDEX mcp_one_active ON mcp_registrations(owner_id,node_id,server) WHERE state='active';
CREATE TABLE mcp_operations (
    operation_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    registration_id TEXT NOT NULL REFERENCES mcp_registrations(registration_id),
    request_json TEXT NOT NULL,
    permission_fingerprint TEXT NOT NULL,
    cancel_cursor INTEGER NOT NULL,
    task_id TEXT UNIQUE REFERENCES tasks(task_id),
    status TEXT NOT NULL DEFAULT 'pending',
    result_json TEXT,
    created_at TEXT NOT NULL
);
