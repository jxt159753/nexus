CREATE TABLE goal_plans (
    goal_id TEXT PRIMARY KEY REFERENCES plugin_goals(goal_id),
    revision INTEGER NOT NULL DEFAULT 1,
    plan_json TEXT NOT NULL,
    replans INTEGER NOT NULL DEFAULT 0,
    resume_after_restart INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0,
    policy_revision INTEGER NOT NULL DEFAULT 0,
    last_reason TEXT NOT NULL DEFAULT '',
    waiting_for_event INTEGER NOT NULL DEFAULT 0,
    waiting_watch_id TEXT,
    last_progress_round INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE goal_steps (
    goal_id TEXT NOT NULL REFERENCES plugin_goals(goal_id),
    step_id TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','running','succeeded','failed','blocked')),
    turn_id TEXT REFERENCES chat_turns(turn_id),
    PRIMARY KEY(goal_id,step_id)
);
CREATE TABLE goal_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES plugin_goals(goal_id),
    revision INTEGER NOT NULL,
    step_id TEXT NOT NULL,
    value_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(goal_id,revision)
);
CREATE TABLE goal_evidence (
    goal_id TEXT NOT NULL REFERENCES plugin_goals(goal_id),
    criterion_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    attempt_id TEXT NOT NULL REFERENCES task_attempts(attempt_id),
    digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(goal_id,criterion_id)
);
CREATE TABLE autonomy_watches (
    watch_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    request_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('file','device','task','external')),
    node_id TEXT,
    target TEXT NOT NULL,
    goal_id TEXT REFERENCES plugin_goals(goal_id),
    priority INTEGER NOT NULL,
    interval_seconds INTEGER NOT NULL,
    max_polls INTEGER NOT NULL,
    polls INTEGER NOT NULL DEFAULT 0,
    next_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_digest TEXT,
    pending_task_id TEXT REFERENCES tasks(task_id),
    scope_digest TEXT,
    permission_fingerprint TEXT NOT NULL,
    cancel_cursor INTEGER NOT NULL,
    paused INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    UNIQUE(owner_id,request_id)
);
CREATE TABLE autonomy_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id TEXT NOT NULL REFERENCES autonomy_watches(watch_id),
    source_key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    UNIQUE(watch_id,source_key)
);
CREATE TABLE autonomy_alerts (
    alert_key TEXT PRIMARY KEY,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    active INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE TABLE cold_payloads (
    table_name TEXT NOT NULL,
    record_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    sha256 TEXT NOT NULL,
    original_bytes INTEGER NOT NULL,
    content BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(table_name,record_id)
);
ALTER TABLE conversations ADD COLUMN cold_archived INTEGER NOT NULL DEFAULT 0;
CREATE TABLE plugin_business_suites (
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    plugin_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    cases_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    rollback_on_failure INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(owner_id,plugin_id)
);
ALTER TABLE plugin_test_runs ADD COLUMN suite_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE plugin_test_runs ADD COLUMN suite_digest TEXT;
ALTER TABLE plugin_test_runs ADD COLUMN independent_cases_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE plugin_test_runs ADD COLUMN baseline_id TEXT REFERENCES plugin_versions(version_id);
CREATE TABLE plugin_rollouts (
    version_id TEXT PRIMARY KEY REFERENCES plugin_versions(version_id),
    baseline_id TEXT NOT NULL REFERENCES plugin_versions(version_id),
    owner_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    permission_fingerprint TEXT NOT NULL,
    cancel_cursor INTEGER NOT NULL,
    suite_revision INTEGER NOT NULL,
    decision_revision INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('observing','quarantined','accepted','rolled_back','blocked')),
    observations INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_task_id TEXT,
    UNIQUE(owner_id,version_id)
);
ALTER TABLE plugin_wakeups ADD COLUMN goal_step_id TEXT;
CREATE TABLE plugin_rollout_tasks (
    version_id TEXT NOT NULL REFERENCES plugin_rollouts(version_id),
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    passed INTEGER NOT NULL,
    PRIMARY KEY(version_id,task_id)
);
