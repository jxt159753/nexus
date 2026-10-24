-- P63 voice sessions. Raw audio is never stored here; only bounded text and evidence.
CREATE TABLE voice_sessions (
    voice_session_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    node_id TEXT NOT NULL REFERENCES identities(identity_id),
    state TEXT NOT NULL CHECK(state IN (
        'activating','listening','thinking','speaking','interrupted','stopping','stopped','error'
    )),
    session_epoch INTEGER NOT NULL CHECK(session_epoch >= 1),
    generation INTEGER NOT NULL DEFAULT 0 CHECK(generation >= 0),
    conversation_id TEXT,
    trigger TEXT NOT NULL CHECK(trigger IN ('button','wake','proactive')),
    input_device TEXT,
    output_device TEXT,
    started_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT,
    metrics_json TEXT,
    UNIQUE(owner_id, request_id)
);

CREATE INDEX voice_sessions_device_state ON voice_sessions(node_id, state);
CREATE INDEX voice_sessions_owner_started ON voice_sessions(owner_id, started_at);

CREATE TABLE voice_utterances (
    utterance_id TEXT PRIMARY KEY,
    voice_session_id TEXT NOT NULL REFERENCES voice_sessions(voice_session_id),
    turn_id TEXT,
    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
    text TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0 CHECK(generation >= 0),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_ms INTEGER NOT NULL DEFAULT 0 CHECK(duration_ms >= 0),
    interrupted INTEGER NOT NULL DEFAULT 0 CHECK(interrupted IN (0,1)),
    barge_in_at TEXT,
    latency_json TEXT
);

CREATE INDEX voice_utterances_session ON voice_utterances(voice_session_id, started_at);
