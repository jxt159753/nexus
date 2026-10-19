ALTER TABLE chat_turns ADD COLUMN phase TEXT NOT NULL DEFAULT 'reply' CHECK(phase IN ('reply','summary'));
ALTER TABLE chat_turns ADD COLUMN native_tool_call_json TEXT;
ALTER TABLE chat_turns ADD COLUMN initial_response_text TEXT;
ALTER TABLE chat_turns ADD COLUMN result_preview_json TEXT;
ALTER TABLE chat_turns ADD COLUMN result_digest TEXT;
ALTER TABLE chat_turns ADD COLUMN summary_consented_at TEXT;
ALTER TABLE model_calls ADD COLUMN chat_stage INTEGER NOT NULL DEFAULT 0 CHECK(chat_stage IN (0,1));
DROP INDEX model_chat_turn;
CREATE UNIQUE INDEX model_chat_stage ON model_calls(chat_turn_id,chat_stage) WHERE chat_turn_id IS NOT NULL;
