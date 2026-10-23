CREATE TABLE memory_policies (
    owner_id TEXT PRIMARY KEY REFERENCES identities(identity_id),
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    revision INTEGER NOT NULL,
    enabled_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE memory_policy_events (
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    revision INTEGER NOT NULL,
    kind TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(owner_id,revision)
);
ALTER TABLE memory_entries ADD COLUMN auto_category TEXT CHECK(auto_category IN ('preference','background','ongoing'));
ALTER TABLE memory_entries ADD COLUMN auto_policy_revision INTEGER;
ALTER TABLE chat_turns ADD COLUMN auto_memory_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE chat_turns ADD COLUMN auto_memory_refs_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE chat_turns ADD COLUMN auto_memory_saved_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE chat_turns ADD COLUMN auto_memory_expires_at TEXT;
