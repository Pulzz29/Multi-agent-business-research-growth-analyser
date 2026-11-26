-- Executive-summary chats (additive): a conversation may be bound to one saved
-- report (run) and the executive-summary version it was opened on.
ALTER TABLE chat_conversations ADD COLUMN IF NOT EXISTS scope_run_id TEXT COLLATE "C";
ALTER TABLE chat_conversations ADD COLUMN IF NOT EXISTS scope_summary_version TEXT COLLATE "C";
CREATE INDEX IF NOT EXISTS ix_chat_conversations_scope ON chat_conversations(user_id, scope_run_id);
