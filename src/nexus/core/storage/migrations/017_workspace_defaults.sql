ALTER TABLE chat_work_grants ADD COLUMN auto_files INTEGER NOT NULL DEFAULT 0 CHECK(auto_files IN (0,1));
ALTER TABLE model_calls ADD COLUMN web_step_id TEXT REFERENCES chat_work_steps(step_id);
CREATE UNIQUE INDEX model_web_step ON model_calls(web_step_id) WHERE web_step_id IS NOT NULL;
