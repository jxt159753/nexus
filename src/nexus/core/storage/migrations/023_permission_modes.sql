CREATE TABLE permission_defaults (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    mode TEXT NOT NULL CHECK(mode IN ('normal','full')),
    revision INTEGER NOT NULL CHECK(revision>=1)
);
INSERT INTO permission_defaults VALUES(1,'normal',1);
ALTER TABLE conversations ADD COLUMN permission_override TEXT CHECK(permission_override IS NULL OR permission_override IN ('normal','full'));
ALTER TABLE conversations ADD COLUMN permission_revision INTEGER NOT NULL DEFAULT 1 CHECK(permission_revision>=1);
ALTER TABLE chat_work_grants ADD COLUMN permission_mode TEXT NOT NULL DEFAULT 'normal' CHECK(permission_mode IN ('normal','full'));
ALTER TABLE chat_work_grants ADD COLUMN permission_fingerprint TEXT NOT NULL DEFAULT '';
CREATE TABLE permission_changes (
    request_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    target TEXT NOT NULL,
    request_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
