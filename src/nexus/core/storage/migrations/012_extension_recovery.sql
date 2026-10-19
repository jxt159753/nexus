-- Preserve every v11 row. Legacy runs have no client request identity.
ALTER TABLE skill_runs ADD COLUMN request_id TEXT;
ALTER TABLE skill_runs ADD COLUMN request_digest TEXT;
CREATE UNIQUE INDEX skill_request_identity ON skill_runs(owner_id,request_id) WHERE request_id IS NOT NULL;

-- Test sessions and each sandbox command are committed before any dispatch.
CREATE TABLE extension_test_runs (
    test_run_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES extension_versions(version_id),
    status TEXT NOT NULL CHECK(status IN ('running','completed','interrupted')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX active_extension_test ON extension_test_runs(version_id) WHERE status='running';
CREATE TABLE sandbox_executions (
    execution_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES extension_versions(version_id),
    node_id TEXT NOT NULL REFERENCES identities(identity_id),
    test_run_id TEXT REFERENCES extension_test_runs(test_run_id),
    artifact_digest TEXT NOT NULL,
    environment_digest TEXT NOT NULL,
    input_json TEXT NOT NULL,
    timeout_seconds INTEGER NOT NULL CHECK(timeout_seconds BETWEEN 1 AND 10),
    command_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('prepared','sent','settled','unknown')),
    result_json TEXT,
    query_count INTEGER NOT NULL DEFAULT 0 CHECK(query_count BETWEEN 0 AND 64),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX sandbox_unsettled ON sandbox_executions(node_id,status);
