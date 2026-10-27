-- Exact lifecycle events from this release onward; old rows are snapshots.
CREATE TABLE ui_extension_events (
    event_id INTEGER PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    kind TEXT NOT NULL,
    from_version_id TEXT REFERENCES ui_extension_versions(version_id),
    to_version_id TEXT REFERENCES ui_extension_versions(version_id),
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX ui_extension_events_conversation ON ui_extension_events(owner_id, conversation_id, event_id);
INSERT INTO ui_extension_events(owner_id,conversation_id,kind,from_version_id,to_version_id,reason,created_at)
SELECT owner_id,conversation_id,'history_snapshot',baseline_id,version_id,status,updated_at
FROM ui_extension_versions ORDER BY rowid;
