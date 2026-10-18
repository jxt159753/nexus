CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL
);
ALTER TABLE tasks ADD COLUMN project_id TEXT REFERENCES projects(project_id);
CREATE TABLE routing_decisions (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
    mode TEXT NOT NULL CHECK(mode IN ('rule','reject')),
    reason_code TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    tool TEXT NOT NULL,
    tool_version INTEGER NOT NULL,
    elapsed_ms REAL,
    recorded_at TEXT NOT NULL,
    origin TEXT NOT NULL CHECK(origin IN ('live','migrated'))
);
INSERT INTO routing_decisions
SELECT task_id, 'rule', CASE json_extract(request_json,'$.intent')
    WHEN 'get_system_info' THEN 'KNOWN_SYSTEM_INFO' ELSE 'KNOWN_DIRECTORY_LIST' END,
    1, json_extract(action_json,'$.tool'), 1, NULL, created_at, 'migrated' FROM tasks;
CREATE TABLE memory_consents (
    consent_event_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    request_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(owner_id,request_id)
);
CREATE TABLE memory_entries (
    memory_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    project_id TEXT REFERENCES projects(project_id),
    consent_event_id TEXT NOT NULL UNIQUE REFERENCES memory_consents(consent_event_id),
    content TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('fact','inference','plan')),
    verification TEXT NOT NULL CHECK(verification IN ('unverified','verified')),
    source TEXT NOT NULL,
    evidence_ref TEXT,
    verified_at TEXT,
    valid_until TEXT,
    supersedes_id TEXT REFERENCES memory_entries(memory_id),
    superseded_at TEXT,
    retracted_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX owner_memories ON memory_entries(owner_id,created_at);
CREATE TABLE model_calls (
    call_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    cost_microunits INTEGER,
    cost_status TEXT NOT NULL CHECK(cost_status IN ('reserved','reported','unknown')),
    created_at TEXT NOT NULL
);
CREATE TABLE client_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_event_id INTEGER UNIQUE REFERENCES task_events(event_id),
    control_event_id INTEGER UNIQUE REFERENCES control_events(event_id),
    CHECK((task_event_id IS NULL) != (control_event_id IS NULL))
);
INSERT INTO client_events(task_event_id,control_event_id)
SELECT task_event_id,control_event_id FROM (
    SELECT event_id AS task_event_id,NULL AS control_event_id,created_at FROM task_events
    UNION ALL SELECT NULL,event_id,created_at FROM control_events
) ORDER BY created_at,task_event_id,control_event_id;
CREATE TRIGGER client_task_event AFTER INSERT ON task_events BEGIN
    INSERT INTO client_events(task_event_id) VALUES(NEW.event_id);
END;
CREATE TRIGGER client_control_event AFTER INSERT ON control_events BEGIN
    INSERT INTO client_events(control_event_id) VALUES(NEW.event_id);
END;
