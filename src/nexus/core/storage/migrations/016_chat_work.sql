CREATE TABLE chat_work_grants (
    grant_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    owner_id TEXT NOT NULL REFERENCES identities(identity_id),
    node_id TEXT NOT NULL REFERENCES identities(identity_id),
    request_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    profile TEXT NOT NULL CHECK(profile IN ('read','standard')),
    scope_digest TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    max_operations INTEGER NOT NULL CHECK(max_operations BETWEEN 1 AND 20),
    used_operations INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    UNIQUE(owner_id,request_id)
);
ALTER TABLE chat_turns ADD COLUMN work_grant_id TEXT REFERENCES chat_work_grants(grant_id);
ALTER TABLE chat_turns ADD COLUMN work_catalog_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE chat_turns ADD COLUMN work_messages_json TEXT;
ALTER TABLE chat_turns ADD COLUMN work_round INTEGER NOT NULL DEFAULT 0;
ALTER TABLE chat_turns ADD COLUMN work_status TEXT NOT NULL DEFAULT '' CHECK(work_status IN ('','waiting','permission','stopping'));
CREATE TABLE chat_work_steps (
    step_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES chat_turns(turn_id),
    step_index INTEGER NOT NULL CHECK(step_index BETWEEN 1 AND 4),
    proposal_json TEXT NOT NULL,
    native_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','waiting','permission','succeeded','failed','stopped')),
    request_text TEXT,
    permission_decision TEXT CHECK(permission_decision IN ('approve','reject')),
    permission_text TEXT,
    task_id TEXT REFERENCES tasks(task_id),
    preview_task_id TEXT REFERENCES tasks(task_id),
    skill_run_id TEXT REFERENCES skill_runs(run_id),
    tool_run_id TEXT REFERENCES extension_tool_runs(tool_run_id),
    result_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(turn_id,step_index)
);
CREATE TABLE chat_work_answers (
    request_id TEXT PRIMARY KEY,
    step_id TEXT NOT NULL REFERENCES chat_work_steps(step_id),
    request_json TEXT NOT NULL
);
CREATE TABLE chat_work_tasks (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
    step_id TEXT NOT NULL REFERENCES chat_work_steps(step_id),
    role TEXT NOT NULL CHECK(role IN ('preview','execute')),
    UNIQUE(step_id,role)
);
CREATE TABLE chat_work_operations (
    grant_id TEXT NOT NULL REFERENCES chat_work_grants(grant_id),
    operation_key TEXT NOT NULL,
    PRIMARY KEY(grant_id,operation_key)
);
ALTER TABLE model_calls ADD COLUMN work_root_turn_id TEXT REFERENCES chat_turns(turn_id);
ALTER TABLE model_calls ADD COLUMN work_round INTEGER;
CREATE UNIQUE INDEX model_work_round ON model_calls(work_root_turn_id,work_round) WHERE work_root_turn_id IS NOT NULL;
ALTER TABLE extension_tool_runs ADD COLUMN work_step_id TEXT REFERENCES chat_work_steps(step_id);
CREATE UNIQUE INDEX tool_work_step ON extension_tool_runs(work_step_id) WHERE work_step_id IS NOT NULL;
