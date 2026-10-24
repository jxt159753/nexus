ALTER TABLE chat_turns ADD COLUMN skill_catalog_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE chat_turns ADD COLUMN skill_plan_json TEXT;
ALTER TABLE chat_turns ADD COLUMN skill_run_id TEXT REFERENCES skill_runs(run_id);
CREATE UNIQUE INDEX chat_skill_run ON chat_turns(skill_run_id) WHERE skill_run_id IS NOT NULL;
ALTER TABLE model_calls ADD COLUMN skill_run_id TEXT REFERENCES skill_runs(run_id);
