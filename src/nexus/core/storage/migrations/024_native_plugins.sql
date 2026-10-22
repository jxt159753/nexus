CREATE TABLE grants_new (
    grant_id TEXT PRIMARY KEY,
    identity_id TEXT NOT NULL REFERENCES identities(identity_id),
    tool TEXT NOT NULL CHECK(tool IN ('system.info','filesystem.list','filesystem.read','filesystem.search','filesystem.write_preview','filesystem.write','command.run','filesystem.read_lines','filesystem.glob','filesystem.grep','filesystem.edit_preview','filesystem.edit','interaction.observe','interaction.act','filesystem.read_image','plugin.call')),
    tool_version INTEGER NOT NULL CHECK(tool_version=1),
    root_id TEXT NOT NULL CHECK(root_id IN ('','workspace')),
    UNIQUE(identity_id,tool,tool_version,root_id),
    CHECK((tool='system.info' AND root_id='') OR (tool!='system.info' AND root_id='workspace'))
);
INSERT INTO grants_new SELECT * FROM grants;
DROP TABLE grants;
ALTER TABLE grants_new RENAME TO grants;
CREATE TABLE plugin_versions (
    version_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    plugin_id TEXT NOT NULL,
    version TEXT NOT NULL,
    digest TEXT NOT NULL,
    release_json TEXT NOT NULL,
    origin TEXT NOT NULL,
    builtin INTEGER NOT NULL CHECK(builtin IN (0,1)),
    test_status TEXT NOT NULL CHECK(test_status IN ('bundled','pending','running','passed','failed','unavailable','interrupted')),
    test_json TEXT NOT NULL DEFAULT '{}',
    approved INTEGER NOT NULL DEFAULT 0 CHECK(approved IN (0,1)),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
    decision_revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(owner_id,plugin_id,digest)
);
CREATE UNIQUE INDEX plugin_one_enabled ON plugin_versions(owner_id,plugin_id) WHERE enabled=1;
CREATE TABLE plugin_decisions (
    decision_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    version_id TEXT NOT NULL REFERENCES plugin_versions(version_id),
    digest TEXT NOT NULL,
    decision TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('normal','full')),
    conversation_id TEXT REFERENCES conversations(conversation_id),
    permission_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE plugin_install_requests (
    request_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    request_json TEXT NOT NULL,
    version_id TEXT NOT NULL REFERENCES plugin_versions(version_id),
    created_at TEXT NOT NULL
);
CREATE TABLE plugin_todos (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(conversation_id),
    value_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE plugin_goals (
    goal_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    objective TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','paused','complete','blocked')),
    max_rounds INTEGER NOT NULL,
    rounds INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    completion_verified INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE plugin_schedules (
    schedule_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    rule_json TEXT NOT NULL,
    next_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    max_runs INTEGER NOT NULL,
    runs INTEGER NOT NULL DEFAULT 0,
    paused INTEGER NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE plugin_schedule_events (
    event_id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL REFERENCES plugin_schedules(schedule_id),
    due_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('due','missed')),
    created_at TEXT NOT NULL,
    UNIQUE(schedule_id,due_at)
);
CREATE TABLE plugin_deliverables (
    deliverable_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    content BLOB NOT NULL,
    created_at TEXT NOT NULL
);
