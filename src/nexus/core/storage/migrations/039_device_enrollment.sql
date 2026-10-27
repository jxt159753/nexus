CREATE TABLE device_clients (
    client_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    node_id TEXT NOT NULL UNIQUE,
    proof_hash TEXT NOT NULL UNIQUE,
    node_token_hash TEXT NOT NULL,
    suggested_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE managed_node_profiles (
    node_id TEXT PRIMARY KEY REFERENCES identities(identity_id),
    description TEXT NOT NULL DEFAULT '',
    policy_json TEXT,
    revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE device_chat_changes (
    turn_id TEXT PRIMARY KEY REFERENCES chat_turns(turn_id),
    node_id TEXT REFERENCES identities(identity_id),
    result_json TEXT NOT NULL
);
CREATE TABLE device_owner_grants (
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    node_id TEXT NOT NULL REFERENCES identities(identity_id),
    tool TEXT NOT NULL CHECK(tool IN ('android.device_status','android.notification_digest','filesystem.receive')),
    tool_version INTEGER NOT NULL CHECK(tool_version=1),
    root_id TEXT NOT NULL,
    PRIMARY KEY(owner_id,node_id,tool,tool_version,root_id)
);
