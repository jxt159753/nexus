CREATE TABLE plugin_test_runs (
    test_run_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES plugin_versions(version_id),
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    work_step_id TEXT REFERENCES chat_work_steps(step_id),
    node_id TEXT NOT NULL,
    environment_json TEXT NOT NULL,
    permission_fingerprint TEXT NOT NULL,
    decision_revision INTEGER NOT NULL,
    cancel_cursor INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','running','passed','failed','unavailable','interrupted','unknown')),
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX plugin_one_test ON plugin_test_runs(version_id) WHERE status IN ('pending','running','unknown');
CREATE TABLE plugin_sandbox_executions (
    execution_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES plugin_versions(version_id),
    node_id TEXT NOT NULL,
    test_run_id TEXT REFERENCES plugin_test_runs(test_run_id),
    artifact_digest TEXT NOT NULL,
    environment_digest TEXT NOT NULL,
    input_json TEXT NOT NULL,
    timeout_seconds INTEGER NOT NULL,
    command_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('prepared','sent','settled','unknown')),
    result_json TEXT,
    query_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
