ALTER TABLE conversations ADD COLUMN project_id TEXT REFERENCES projects(project_id);
ALTER TABLE chat_turns ADD COLUMN memory_refs_json TEXT NOT NULL DEFAULT '[]';
CREATE INDEX conversation_project ON conversations(owner_id,project_id);
CREATE TABLE project_todos (
    todo_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    request_id TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('planned','done','cancelled')),
    evidence_task_id TEXT REFERENCES tasks(task_id),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_id,request_id)
);
CREATE TABLE project_todo_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    todo_id TEXT NOT NULL REFERENCES project_todos(todo_id),
    request_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(todo_id,request_id)
);
