ALTER TABLE tasks ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0,1));
ALTER TABLE task_attempts ADD COLUMN execution_stopped INTEGER NOT NULL DEFAULT 0 CHECK (execution_stopped IN (0,1));
ALTER TABLE task_attempts ADD COLUMN stop_reason TEXT;
ALTER TABLE task_attempts ADD COLUMN probe_count INTEGER NOT NULL DEFAULT 0 CHECK (probe_count BETWEEN 0 AND 64);
UPDATE task_attempts SET execution_stopped=1 WHERE phase='completed';
CREATE TABLE system_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton=1),
    paused INTEGER NOT NULL CHECK (paused IN (0,1)),
    revision INTEGER NOT NULL CHECK (revision>=0)
);
INSERT INTO system_state VALUES (1,0,0);
CREATE TABLE control_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    kind TEXT NOT NULL,
    paused INTEGER NOT NULL,
    revision INTEGER NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE control_requests (
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    request_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('pause','resume','cancel_all')),
    response_json TEXT NOT NULL,
    PRIMARY KEY(owner_id, request_id)
);
