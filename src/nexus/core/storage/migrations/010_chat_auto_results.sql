ALTER TABLE chat_turns ADD COLUMN auto_result_json TEXT;
ALTER TABLE chat_turns ADD COLUMN auto_result_status TEXT NOT NULL DEFAULT 'none' CHECK(auto_result_status IN ('none','waiting','queued','stopped','unavailable'));
DROP INDEX chat_one_active;
CREATE UNIQUE INDEX chat_one_active ON chat_turns(conversation_id) WHERE status IN ('queued','generating','stopping') OR auto_result_status='waiting';
