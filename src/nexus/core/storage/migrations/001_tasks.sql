CREATE TABLE identities (
    identity_id TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('owner', 'node')),
    token_hash TEXT NOT NULL UNIQUE,
    revoked_at TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX single_owner ON identities(role) WHERE role = 'owner';
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    request_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    node_id TEXT NOT NULL REFERENCES identities(identity_id),
    action_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'awaiting_approval', 'ready', 'dispatching', 'running', 'recovering', 'verifying', 'succeeded', 'failed', 'cancelled', 'timed_out')),
    execution_outcome TEXT NOT NULL DEFAULT 'not_started' CHECK (execution_outcome IN ('not_started', 'known', 'unknown')),
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE (owner_id, request_id)
);
CREATE TABLE task_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    revision INTEGER NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (task_id, revision)
);
CREATE TABLE audit_events (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id TEXT REFERENCES identities(identity_id),
    subject_id TEXT REFERENCES identities(identity_id),
    task_id TEXT REFERENCES tasks(task_id),
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX task_owner_history ON tasks(owner_id, created_at, task_id);
CREATE INDEX task_event_history ON task_events(task_id, event_id);
