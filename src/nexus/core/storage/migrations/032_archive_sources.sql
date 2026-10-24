-- Snapshots stay logically immutable. Moving their bytes requires a saved cold
-- payload in the same transaction; hashes and all identity columns stay fixed.
DROP TRIGGER chat_request_immutable;
CREATE TRIGGER chat_request_immutable BEFORE UPDATE OF call_id,turn_id,request_sha256,created_at ON chat_exchanges
BEGIN SELECT RAISE(ABORT,'model request identities are immutable'); END;
CREATE TRIGGER chat_request_payload_immutable BEFORE UPDATE OF request_body ON chat_exchanges
WHEN NOT (
    (OLD.request_body='' OR NEW.request_body='') AND OLD.request_body!=NEW.request_body
    AND EXISTS(SELECT 1 FROM cold_payloads WHERE table_name='chat_exchanges' AND record_id=OLD.call_id)
)
BEGIN SELECT RAISE(ABORT,'model request snapshots require verified archival'); END;
DROP TRIGGER chat_compaction_source_immutable;
CREATE TRIGGER chat_compaction_source_immutable BEFORE UPDATE OF call_id,turn_id,source_ids_json,source_digest,surface_digest,prefix_count,before_tokens,created_at ON chat_compactions
BEGIN SELECT RAISE(ABORT,'compaction identities are immutable'); END;
CREATE TRIGGER chat_compaction_payload_immutable BEFORE UPDATE OF source_messages_json ON chat_compactions
WHEN NOT (
    (OLD.source_messages_json='' OR NEW.source_messages_json='') AND OLD.source_messages_json!=NEW.source_messages_json
    AND EXISTS(SELECT 1 FROM cold_payloads WHERE table_name='chat_compactions' AND record_id=OLD.call_id)
)
BEGIN SELECT RAISE(ABORT,'compaction sources require verified archival'); END;
