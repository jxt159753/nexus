DROP INDEX chat_one_active;
CREATE UNIQUE INDEX chat_one_active ON chat_turns(conversation_id) WHERE status IN ('generating','stopping');
ALTER TABLE chat_turns ADD COLUMN queue_after_turn_id TEXT REFERENCES chat_turns(turn_id);
ALTER TABLE chat_turns ADD COLUMN recovery_required INTEGER NOT NULL DEFAULT 0 CHECK(recovery_required IN (0,1));
ALTER TABLE chat_turns ADD COLUMN history_summary_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE chat_turns ADD COLUMN references_json TEXT NOT NULL DEFAULT '[]';
CREATE TABLE chat_exchanges (
    call_id TEXT PRIMARY KEY REFERENCES model_calls(call_id),
    turn_id TEXT NOT NULL REFERENCES chat_turns(turn_id),
    request_body TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT,
    partial_text TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT 'started' CHECK(outcome IN ('started','completed','failed','stopped','interrupted','unknown')),
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX chat_exchange_turn ON chat_exchanges(turn_id);
CREATE TRIGGER chat_request_immutable BEFORE UPDATE OF call_id,turn_id,request_body,request_sha256,created_at ON chat_exchanges
BEGIN SELECT RAISE(ABORT,'model request snapshots are immutable'); END;
CREATE TABLE chat_controls (
    control_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES chat_turns(turn_id),
    request_id TEXT NOT NULL UNIQUE,
    request_json TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('steer','resume','stop')),
    text TEXT NOT NULL DEFAULT '',
    applied_call_id TEXT REFERENCES model_calls(call_id),
    created_at TEXT NOT NULL
);
CREATE INDEX chat_control_turn ON chat_controls(turn_id);
