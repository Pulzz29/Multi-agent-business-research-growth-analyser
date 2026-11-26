-- Byte-order ("C") collation for the text columns added in 0007/0008, as for
-- every other text column (0003/0005). Type and data are unchanged.
ALTER TABLE chat_messages ALTER COLUMN content TYPE TEXT COLLATE "C";
ALTER TABLE chat_messages ALTER COLUMN payload_json TYPE TEXT COLLATE "C";
ALTER TABLE error_events ALTER COLUMN message TYPE TEXT COLLATE "C";
ALTER TABLE error_events ALTER COLUMN stack_trace TYPE TEXT COLLATE "C";
ALTER TABLE ai_governance_events ALTER COLUMN violations_json TYPE TEXT COLLATE "C";
ALTER TABLE ai_governance_events ALTER COLUMN evidence_json TYPE TEXT COLLATE "C";
