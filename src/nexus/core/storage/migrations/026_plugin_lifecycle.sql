ALTER TABLE conversations ADD COLUMN plan_mode INTEGER NOT NULL DEFAULT 0 CHECK(plan_mode IN (0,1));
CREATE TABLE plugin_authorities (
    authority_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('goal','schedule')),
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    root_turn_id TEXT NOT NULL REFERENCES chat_turns(turn_id),
    last_turn_id TEXT NOT NULL REFERENCES chat_turns(turn_id),
    node_id TEXT NOT NULL,
    profile TEXT NOT NULL,
    scope_digest TEXT NOT NULL,
    catalog_digest TEXT NOT NULL,
    permission_fingerprint TEXT NOT NULL,
    cancel_cursor INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','paused','interrupted'))
);
CREATE TABLE plugin_wakeups (
    wakeup_id TEXT PRIMARY KEY,
    authority_id TEXT NOT NULL REFERENCES plugin_authorities(authority_id),
    request_json TEXT NOT NULL,
    turn_id TEXT REFERENCES chat_turns(turn_id),
    status TEXT NOT NULL CHECK(status IN ('prepared','submitted','interrupted')),
    created_at TEXT NOT NULL
);
CREATE TABLE plugin_control_requests (
    request_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
