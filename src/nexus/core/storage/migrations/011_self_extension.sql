-- M4 self-extension. Immutable candidate releases, separate test/approval/
-- enablement state, and bounded declarative Skill runs.
CREATE TABLE extension_requests (
    request_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    kind TEXT NOT NULL CHECK(kind IN ('tool','skill')),
    need TEXT NOT NULL,
    capability_id TEXT,
    node_id TEXT REFERENCES identities(identity_id),
    parent_version_id TEXT,
    request_json TEXT NOT NULL,
    outbound_json TEXT NOT NULL,
    preview_digest TEXT NOT NULL,
    preview_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','queued','generating','candidate','failed','cancelled')),
    mode TEXT CHECK(mode IN ('fixture','llm')),
    error_code TEXT,
    candidate_version_id TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_id,request_id)
);
CREATE INDEX extension_request_owner ON extension_requests(owner_id,created_at);
CREATE TABLE extension_versions (
    version_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES extension_requests(request_id),
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    kind TEXT NOT NULL CHECK(kind IN ('tool','skill')),
    capability_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision>=1),
    parent_version_id TEXT REFERENCES extension_versions(version_id),
    manifest_json TEXT NOT NULL,
    source_text TEXT,
    markdown_text TEXT,
    steps_json TEXT,
    tests_json TEXT NOT NULL,
    release_json TEXT NOT NULL,
    artifact_digest TEXT NOT NULL,
    environment_json TEXT NOT NULL,
    environment_digest TEXT NOT NULL,
    test_status TEXT NOT NULL DEFAULT 'untested' CHECK(test_status IN ('untested','passed','failed','unavailable')),
    approval_status TEXT NOT NULL DEFAULT 'none' CHECK(approval_status IN ('none','approved','rejected')),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(owner_id,capability_id,revision)
);
CREATE INDEX extension_version_owner ON extension_versions(owner_id,created_at);
CREATE INDEX extension_version_capability ON extension_versions(owner_id,capability_id,revision);
CREATE TABLE extension_tests (
    test_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES extension_versions(version_id),
    artifact_digest TEXT NOT NULL,
    environment_digest TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('skill_standin','sandbox')),
    status TEXT NOT NULL CHECK(status IN ('passed','failed','unavailable')),
    controller_version TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX extension_test_version ON extension_tests(version_id,created_at);
CREATE TABLE extension_approvals (
    approval_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES extension_versions(version_id),
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    artifact_digest TEXT NOT NULL,
    test_id TEXT NOT NULL REFERENCES extension_tests(test_id),
    environment_digest TEXT NOT NULL,
    node_id TEXT REFERENCES identities(identity_id),
    resource_scope TEXT NOT NULL,
    parameters_digest TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approve','reject')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX extension_approval_version ON extension_approvals(version_id,created_at);
CREATE TABLE extension_enablements (
    version_id TEXT PRIMARY KEY REFERENCES extension_versions(version_id),
    approval_id TEXT NOT NULL REFERENCES extension_approvals(approval_id),
    enabled_by TEXT NOT NULL REFERENCES identities(identity_id),
    enabled_at TEXT NOT NULL,
    disabled_at TEXT,
    disable_reason TEXT
);
CREATE TABLE skill_runs (
    run_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    version_id TEXT NOT NULL REFERENCES extension_versions(version_id),
    approval_id TEXT NOT NULL REFERENCES extension_approvals(approval_id),
    artifact_digest TEXT NOT NULL,
    environment_digest TEXT NOT NULL,
    node_id TEXT NOT NULL REFERENCES identities(identity_id),
    input_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','cancelling','succeeded','failed','cancelled','timed_out')),
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
    current_step INTEGER NOT NULL DEFAULT 1 CHECK(current_step>=1),
    result_json TEXT,
    error_json TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX skill_run_owner ON skill_runs(owner_id,created_at);
CREATE INDEX skill_run_active ON skill_runs(status,expires_at);
CREATE TABLE skill_run_steps (
    run_id TEXT NOT NULL REFERENCES skill_runs(run_id),
    step_index INTEGER NOT NULL CHECK(step_index>=1),
    step_id TEXT NOT NULL,
    task_id TEXT REFERENCES tasks(task_id),
    tool_run_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending','dispatchable','approval','running','succeeded','failed','unknown','cancelled')),
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(run_id,step_index)
);
CREATE INDEX skill_step_task ON skill_run_steps(task_id);
CREATE TABLE extension_tool_runs (
    tool_run_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    version_id TEXT NOT NULL REFERENCES extension_versions(version_id),
    approval_id TEXT NOT NULL REFERENCES extension_approvals(approval_id),
    node_id TEXT NOT NULL REFERENCES identities(identity_id),
    skill_run_id TEXT REFERENCES skill_runs(run_id),
    step_index INTEGER,
    artifact_digest TEXT NOT NULL,
    environment_digest TEXT NOT NULL,
    input_json TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('dispatching','succeeded','failed','unknown','cancelled')),
    output_json TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(skill_run_id,step_index)
);
CREATE INDEX extension_tool_run_owner ON extension_tool_runs(owner_id,created_at);
ALTER TABLE model_calls ADD COLUMN extension_request_id TEXT REFERENCES extension_requests(request_id);
CREATE UNIQUE INDEX model_extension_request ON model_calls(extension_request_id) WHERE extension_request_id IS NOT NULL;
