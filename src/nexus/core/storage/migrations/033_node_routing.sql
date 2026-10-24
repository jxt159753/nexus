-- One conversation can use several existing device scopes without merging them.
ALTER TABLE chat_work_grants ADD COLUMN parent_grant_id TEXT REFERENCES chat_work_grants(grant_id);
CREATE INDEX work_grant_parent ON chat_work_grants(parent_grant_id);
ALTER TABLE chat_work_steps ADD COLUMN routed_grant_id TEXT REFERENCES chat_work_grants(grant_id);
CREATE TABLE node_names (
    node_id TEXT PRIMARY KEY REFERENCES identities(identity_id),
    name TEXT NOT NULL COLLATE NOCASE UNIQUE CHECK(length(name) BETWEEN 1 AND 40)
);
