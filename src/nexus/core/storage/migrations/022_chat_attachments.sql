CREATE TABLE chat_attachments (
    attachment_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    request_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    claimed_media_type TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 0 AND 16777216),
    sha256 TEXT NOT NULL,
    content BLOB,
    preview_json TEXT,
    created_at TEXT NOT NULL,
    deleted_at TEXT,
    UNIQUE(owner_id, request_id),
    CHECK((deleted_at IS NULL AND content IS NOT NULL AND length(content)=size_bytes AND preview_json IS NOT NULL)
       OR (deleted_at IS NOT NULL AND content IS NULL AND preview_json IS NULL))
);
CREATE INDEX attachment_conversation ON chat_attachments(conversation_id,created_at);
CREATE TRIGGER attachment_identity_immutable BEFORE UPDATE OF attachment_id,conversation_id,owner_id,request_id,filename,claimed_media_type,media_type,size_bytes,sha256,created_at ON chat_attachments
BEGIN SELECT RAISE(ABORT,'attachment identity is immutable'); END;
ALTER TABLE chat_turns ADD COLUMN attachments_json TEXT NOT NULL DEFAULT '[]';
CREATE TABLE chat_turn_attachments (
    turn_id TEXT NOT NULL REFERENCES chat_turns(turn_id),
    attachment_id TEXT NOT NULL REFERENCES chat_attachments(attachment_id),
    PRIMARY KEY(turn_id,attachment_id)
);
