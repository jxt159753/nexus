-- Keep step identity and references while removing the cumulative action quota.
CREATE TABLE chat_work_steps_new (
    step_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES chat_turns(turn_id),
    step_index INTEGER NOT NULL CHECK(step_index>=1),
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
    routed_grant_id TEXT REFERENCES chat_work_grants(grant_id),
    UNIQUE(turn_id,step_index)
);
INSERT INTO chat_work_steps_new SELECT * FROM chat_work_steps;
DROP TABLE chat_work_steps;
ALTER TABLE chat_work_steps_new RENAME TO chat_work_steps;

-- Historical grants keep their original policy; new grants record the owner configuration.
ALTER TABLE chat_work_grants ADD COLUMN operation_limits_enabled INTEGER NOT NULL DEFAULT 1 CHECK(operation_limits_enabled IN (0,1));
