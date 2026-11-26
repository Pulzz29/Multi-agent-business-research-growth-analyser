-- Re-applies the byte-wise ("C") collation to text columns added after
-- 0003 (stored_documents.content / sha256 etc.). Idempotent: columns that
-- already use "C" are skipped.
DO $$
DECLARE
    col RECORD;
BEGIN
    FOR col IN
        SELECT c.table_name, c.column_name
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON t.table_schema = c.table_schema AND t.table_name = c.table_name
        WHERE c.table_schema = current_schema()
          AND t.table_type = 'BASE TABLE'
          AND c.data_type = 'text'
          AND COALESCE(c.collation_name, '') <> 'C'
    LOOP
        EXECUTE format('ALTER TABLE %I ALTER COLUMN %I TYPE TEXT COLLATE "C"',
                       col.table_name, col.column_name);
    END LOOP;
END
$$;
