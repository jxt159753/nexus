ALTER TABLE conversations ADD COLUMN auto_title INTEGER NOT NULL DEFAULT 0 CHECK(auto_title IN (0,1));
ALTER TABLE chat_turns ADD COLUMN proposal_json TEXT;
ALTER TABLE chat_turns ADD COLUMN proposal_digest TEXT;
ALTER TABLE chat_turns ADD COLUMN proposal_policy_version INTEGER;
ALTER TABLE chat_turns ADD COLUMN action_status TEXT NOT NULL DEFAULT 'none' CHECK(action_status IN ('none','proposed','rejected','submitted'));
ALTER TABLE chat_turns ADD COLUMN task_id TEXT REFERENCES tasks(task_id);
CREATE UNIQUE INDEX chat_action_task ON chat_turns(task_id) WHERE task_id IS NOT NULL;
