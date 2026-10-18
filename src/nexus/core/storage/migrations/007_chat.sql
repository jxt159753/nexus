CREATE TABLE conversations (
    conversation_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    request_id TEXT NOT NULL,
    title TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
    created_at TEXT NOT NULL,
    UNIQUE(owner_id,request_id)
);
CREATE TABLE chat_turns (
    turn_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    request_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    text TEXT NOT NULL,
    context_json TEXT NOT NULL,
    context_digest TEXT NOT NULL,
    context_count INTEGER NOT NULL,
    context_truncated INTEGER NOT NULL CHECK(context_truncated IN (0,1)),
    response_text TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK(status IN ('queued','generating','completed','incomplete','stopping','stopped','interrupted','failed','expired')),
    error_code TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE(owner_id,request_id)
);
CREATE UNIQUE INDEX chat_one_active ON chat_turns(conversation_id) WHERE status IN ('queued','generating','stopping');
CREATE INDEX chat_queue ON chat_turns(status,created_at);
CREATE INDEX chat_history ON chat_turns(conversation_id,created_at);
ALTER TABLE model_calls ADD COLUMN chat_turn_id TEXT REFERENCES chat_turns(turn_id);
CREATE UNIQUE INDEX model_chat_turn ON model_calls(chat_turn_id) WHERE chat_turn_id IS NOT NULL;
