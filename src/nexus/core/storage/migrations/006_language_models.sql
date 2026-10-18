CREATE TABLE language_requests (
    language_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    request_id TEXT NOT NULL,
    node_id TEXT NOT NULL REFERENCES identities(identity_id),
    project_id TEXT REFERENCES projects(project_id),
    request_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','interpreting','proposed','rejected','failed','cancelled','expired','submitted')),
    mode TEXT CHECK(mode IN ('rule','llm')),
    proposal_json TEXT,
    error_code TEXT,
    task_id TEXT REFERENCES tasks(task_id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE(owner_id,request_id)
);
CREATE INDEX language_queue ON language_requests(status,created_at);
ALTER TABLE model_calls RENAME TO model_calls_m1;
CREATE TABLE model_calls (
    call_id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES tasks(task_id),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    cost_microunits INTEGER CHECK(cost_microunits>=0),
    cost_status TEXT NOT NULL CHECK(cost_status IN ('reserved','reported','calculated','unknown')),
    created_at TEXT NOT NULL,
    language_id TEXT UNIQUE REFERENCES language_requests(language_id),
    owner_id TEXT REFERENCES identities(identity_id),
    reserved_microunits INTEGER NOT NULL DEFAULT 0 CHECK(reserved_microunits>=0),
    input_tokens INTEGER,
    output_tokens INTEGER,
    pricing_json TEXT,
    finished_at TEXT,
    outcome TEXT NOT NULL DEFAULT 'unknown' CHECK(outcome IN ('started','completed','unknown'))
);
INSERT INTO model_calls(call_id,task_id,provider,model,cost_microunits,cost_status,created_at)
SELECT call_id,task_id,provider,model,cost_microunits,cost_status,created_at FROM model_calls_m1;
DROP TABLE model_calls_m1;
CREATE INDEX model_budget_day ON model_calls(created_at,cost_status);
