-- Client code runs in an opaque browser sandbox, never in Core or the Node host.
CREATE TABLE ui_extension_policies (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(conversation_id),
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    client_id TEXT NOT NULL,
    plugin_id TEXT NOT NULL,
    profile TEXT NOT NULL,
    revision INTEGER NOT NULL,
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    active_version_id TEXT,
    deployment_revision INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE ui_extension_versions (
    version_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    work_step_id TEXT NOT NULL UNIQUE REFERENCES chat_work_steps(step_id),
    plugin_id TEXT NOT NULL,
    version TEXT NOT NULL,
    policy_revision INTEGER NOT NULL,
    baseline_id TEXT REFERENCES ui_extension_versions(version_id),
    bundle_json TEXT NOT NULL,
    bundle_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('built','testing','failed','interrupted','observing','accepted','rolled_back','disabled')),
    lease_id TEXT,
    lease_expires_at TEXT,
    report_json TEXT,
    runtime_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_id,conversation_id,plugin_id,version)
);
CREATE TABLE ui_extension_clients (
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    client_id TEXT NOT NULL,
    runtime TEXT NOT NULL,
    platform TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY(owner_id,client_id)
);
