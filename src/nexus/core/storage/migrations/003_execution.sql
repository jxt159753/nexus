CREATE TABLE execution_grants (
    grant_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    node_id TEXT NOT NULL REFERENCES identities(identity_id),
    policy_version INTEGER NOT NULL,
    scope_digest TEXT NOT NULL,
    approval_id TEXT REFERENCES approvals(approval_id),
    command_digest TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE task_attempts (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    node_id TEXT NOT NULL REFERENCES identities(identity_id),
    grant_id TEXT NOT NULL UNIQUE REFERENCES execution_grants(grant_id),
    attempt_no INTEGER NOT NULL CHECK (attempt_no BETWEEN 1 AND 2),
    dispatch_count INTEGER NOT NULL CHECK (dispatch_count BETWEEN 1 AND 3),
    phase TEXT NOT NULL CHECK (phase IN ('dispatching','running','verifying','completed','unknown')),
    command_json TEXT NOT NULL,
    command_digest TEXT NOT NULL,
    boot_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    result_json TEXT,
    UNIQUE(task_id, attempt_no)
);
ALTER TABLE tasks ADD COLUMN current_attempt_id TEXT REFERENCES task_attempts(attempt_id);
ALTER TABLE tasks ADD COLUMN result_json TEXT;
ALTER TABLE tasks ADD COLUMN error_json TEXT;
ALTER TABLE tasks ADD COLUMN verification TEXT CHECK (verification = 'node_report_schema_checked');
CREATE INDEX pending_tasks ON tasks(node_id, status);
CREATE UNIQUE INDEX one_active_attempt_per_node ON task_attempts(node_id) WHERE phase != 'completed';
