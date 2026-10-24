ALTER TABLE chat_turns ADD COLUMN context_surface_json TEXT;
ALTER TABLE chat_turns ADD COLUMN context_sources_json TEXT NOT NULL DEFAULT '[]';
CREATE TABLE chat_compactions (
    call_id TEXT PRIMARY KEY REFERENCES model_calls(call_id),
    turn_id TEXT NOT NULL UNIQUE REFERENCES chat_turns(turn_id),
    source_messages_json TEXT NOT NULL,
    source_ids_json TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    surface_digest TEXT NOT NULL,
    prefix_count INTEGER NOT NULL,
    before_tokens INTEGER NOT NULL,
    after_tokens INTEGER,
    summary_text TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT 'started' CHECK(outcome IN ('started','completed','failed','stopped','interrupted')),
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TRIGGER chat_compaction_source_immutable BEFORE UPDATE OF call_id,turn_id,source_messages_json,source_ids_json,source_digest,surface_digest,prefix_count,before_tokens,created_at ON chat_compactions
BEGIN SELECT RAISE(ABORT,'compaction sources are immutable'); END;
