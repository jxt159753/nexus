CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    token_hash TEXT NOT NULL UNIQUE,
    csrf_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE policy_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL CHECK (version >= 1)
);
INSERT INTO policy_state VALUES (1, 1);
CREATE TABLE grants (
    grant_id TEXT PRIMARY KEY,
    identity_id TEXT NOT NULL REFERENCES identities(identity_id),
    tool TEXT NOT NULL CHECK (tool IN ('system.info', 'filesystem.list')),
    tool_version INTEGER NOT NULL CHECK (tool_version = 1),
    root_id TEXT NOT NULL CHECK (root_id IN ('', 'workspace')),
    UNIQUE (identity_id, tool, tool_version, root_id),
    CHECK ((tool = 'system.info' AND root_id = '') OR (tool = 'filesystem.list' AND root_id = 'workspace'))
);
CREATE TABLE approvals (
    approval_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    node_id TEXT NOT NULL REFERENCES identities(identity_id),
    tool TEXT NOT NULL,
    tool_version INTEGER NOT NULL,
    arguments_digest TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    scope_digest TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decision TEXT CHECK (decision IN ('approve', 'reject')),
    decided_by TEXT REFERENCES identities(identity_id),
    decided_at TEXT
);
CREATE INDEX task_approval_history ON approvals(task_id, policy_version, scope_digest);
