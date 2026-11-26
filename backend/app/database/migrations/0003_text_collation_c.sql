-- Byte-wise ("C") collation for every text column of this schema, so that
-- ORDER BY, MIN/MAX and comparisons of text (ISO timestamps, tickers, JSON
-- text, identifiers) behave exactly as they did in SQLite, independent of
-- the server's locale (for example English_United States.1252 on Windows or
-- en_US.UTF-8 on a cloud server). "C" is built into every PostgreSQL server.
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
