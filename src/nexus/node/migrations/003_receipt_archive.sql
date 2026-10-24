CREATE TABLE receipt_archives (
    attempt_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    original_bytes INTEGER NOT NULL,
    content BLOB NOT NULL
);
