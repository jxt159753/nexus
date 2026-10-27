ALTER TABLE plugin_schedules ADD COLUMN run_until_stopped INTEGER NOT NULL DEFAULT 0 CHECK(run_until_stopped IN (0,1));
ALTER TABLE plugin_schedules ADD COLUMN resume_after_restart INTEGER NOT NULL DEFAULT 0 CHECK(resume_after_restart IN (0,1));
ALTER TABLE plugin_schedules ADD COLUMN targets_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE plugin_goals ADD COLUMN run_until_stopped INTEGER NOT NULL DEFAULT 0 CHECK(run_until_stopped IN (0,1));
ALTER TABLE plugin_goals ADD COLUMN resume_after_restart INTEGER NOT NULL DEFAULT 0 CHECK(resume_after_restart IN (0,1));

CREATE TABLE reminder_clients (
    client_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    token_hash TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    platform TEXT NOT NULL CHECK(platform IN ('android','windows','web')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    created_at TEXT NOT NULL,
    last_seen_at TEXT
);
CREATE TABLE reminders (
    reminder_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    source_key TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read_at TEXT,
    targets_json TEXT NOT NULL,
    UNIQUE(owner_id,source_key)
);
CREATE TABLE reminder_deliveries (
    reminder_id TEXT NOT NULL REFERENCES reminders(reminder_id),
    client_id TEXT NOT NULL REFERENCES reminder_clients(client_id),
    posted_at TEXT,
    PRIMARY KEY(reminder_id,client_id)
);
CREATE INDEX reminder_deliveries_pending ON reminder_deliveries(client_id,posted_at);
CREATE INDEX reminders_owner ON reminders(owner_id,created_at);
